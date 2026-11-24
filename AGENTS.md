# AGENTS.md — Conventions du projet code-pipeline

Ce fichier est lu automatiquement par les agents (Vibe Code CLI, VS Code
extension, agents du pipeline). Il condense toutes les décisions prises
lors de la conception du projet — respecte-les sans les rediscuter.

## Objet

Pipeline multi-agents autonome de développement, exécuté sur **Mistral
Workflows** (orchestration durable Temporal) :
**spec (interview développeur + SPEC.md) → gate humaine → plan → dev parallèle (git worktrees) →
boucle tests bornée → fusions séquentielles → gate humaine finale → MR ouverte (merge manuel sur le forge)**.

## Stack et dépendances

- Python 3.12, `mistralai-workflows[mistralai]` (plugin Mistral).
- Agents exécutés via l'**Agents SDK** dans des activités : un objet `Agent`
  ne traverse pas Temporal, seul son nom/paramètres passent. Ne PAS utiliser
  les durable agents (`Agent`/`Runner` du plugin) : dépréciés.
- Routing modèles : `mistral-large-latest` pour le raisonnement
  (spec/planner/reviewer), `mistral-medium-latest` pour les boucles dev/tests.
- Repo cible quelconque. La spec est un fichier `SPEC.md` à la racine du repo
  cible : l'agent spec l'écrit après avoir interviewé le développeur
  (QUESTION:/reply), et le développeur la valide à la gate.

## Règles d'architecture (non négociables)

1. **Spec vérifiable** : chaque requirement a au moins un scénario WHEN/THEN
   testable. Pas de scénario, pas de requirement.
2. **Isolation** : une tâche dev = un git worktree = une branche `agent/<task_id>`.
   Jamais de commit directement sur main par un agent.
3. **Fusions séquentielles** : une branche à la fois, suite de tests complète
   re-jouée après CHAQUE fusion. Conflit → abort + escalade. Si la suite ne peut
   pas tourner dans le worker (OOM killer même en `--runInBand`, ou outil de test
   introuvable genre `jest: not found` après un `npm ci` interrompu — RAM fixée
   par la plateforme), la gate de tests est NON bloquante : on poursuit jusqu'au
   push de la branche `ia-generated/*` + MR, et c'est la CI de la MR qui valide.
4. **Boucles bornées** : `MAX_DEV_ITERATIONS = 3` (dev→tests), `MAX_AGENT_TURNS
   = 20` (boucle d'outils). Au-delà : escalade humaine, jamais de boucle infinie.
5. **Gates humaines** : (a) approbation de `SPEC.md`, (b) review du
   diff final. À la gate finale, un signal `reply` = retour de review : le
   pipeline corrige la branche, re-push la MR et redemande (`MAX_REVIEW_ROUNDS = 3`
   tours max, puis escalade). Le workflow dort sur un signal (`approve`/`reject`) —
   voir Waiting for Conditions dans la doc Mistral Workflows. **Interview
   interactif** : pendant la phase spec, l'agent pose des questions
   (signal `reply` via `scripts/talk.py`, transcript lisible via la query
   `state` / `scripts/status.py`), sans limite de tours (attente 24 h par question ; escalade après `MAX_EMPTY_SPEC_ROUNDS = 3` tours sans progression).
6. **Interfaces d'abord** : le planner place les types/API partagés dans la
   première tâche ; les tâches parallèles codent contre ce contrat.
7. **Tools confinés** : les tools shell/fichiers des agents dev sont bornés au
   worktree (pas de path traversal, pas de `sudo`, pas de `git push`).
8. **Pas de hors-scope** : un agent dev n'implémente QUE sa tâche ; le
   reviewer rejette tout hunk hors `files_hint`.

## Repos cibles

- Front Angular : `C:\projets\toollive-c8y` (alias `c8y` dans `scripts/run.py`) —
  tests Jest auto-détectés (`npm run test-unit -- --ci`), lint `npm run lint`,
  branche de base `develop`.
- Microservice Python : `C:\projets\toollive-c8y-microservice` (alias `micro`) —
  tests `uv run pytest -x -q`, lint `uv run ruff check`, branche de base
  `develop`.

Détail et variables d'env : `docs/architecture.md`. La commande de test est
auto-détectée par repo (`test_runner.py::test_cmd_for`, override
`pipeline_test_cmd`) ; la branche de base est auto-détectée
(`git_tools.py::base_branch`, override `pipeline_base_branch`).

En mode cloud, `--repo` accepte une **URL GitLab** (résolue aussi via
`gitlab_c8y_repo` / `gitlab_micro_repo`) : le worker clone le repo cible
(`git_tools.ensure_workspace`, token `gitlab_token`), pousse
`ia-generated/<change_id>` et ouvre une **Merge Request GitLab** (merge manuel après
approbation — pas de merge automatique). Pour **compléter une MR en cours**,
mettre « MR <iid> » dans la demande (ex. `repars de la MR 1585 : ...`) : le
pipeline fetch la branche source de la MR, travaille dessus, et push dessus à
la fin — la MR est mise à jour, jamais supprimée ni recréée. Les repos cibles restent sur GitLab, inchangés.

Gates humaines : signaux `approve`/`reject` sur le workflow
(`@workflows.workflow.signal` + `wait_condition` dans `spec_to_tests.py`) ;
réponse via `python scripts/approve.py <execution_id> approve|reject` ;
notification via `open_approval` (webhook `approval_webhook_url`).

## Intégration JIRA (connecteur MCP `atlassian`)

- `python scripts/run.py "..." --repo c8y --jira TOOLLIVE-3600` (input
  `jira_issue` du workflow `spec-to-tests`) : le ticket est lu via le connecteur
  MCP `atlassian` — slot `atlassian_connector` déclaré dans
  `activities/jira_tools.py`, attaché au workflow par `@uses_connectors` (placé
  APRÈS `@workflow.define`). Activités : `fetch_jira_issue` / `jira_search` /
  `jira_comment` / `jira_transition(_start|_review|_done)` (tools MCP
  `getJiraIssue`, `searchJiraIssuesUsingJql`, `getTransitionsForJiraIssue`,
  `addCommentToJiraIssue`, `transitionJiraIssue`).
- Le contenu du ticket (description + commentaires) est prépendu à la demande de
  l'agent spec ; chaque gate est commentée sur le ticket ; à l'ouverture de la
  MR, transition + commentaire avec le lien MR ; commentaire final en sortie.
- **Workflow JIRA v4 avec gardes** (`activities/jira_workflow.py`, activité
  `jira_move`) : un statut par phase (Pris en charge → Specs → ready-for-dev →
  devs-en-cours → Tests auto → code-review ⇄ code-review-in-progress), `blocked`
  + motif sur toute sortie en échec. Garde côté code : transition absente du
  tableau v4 ou réservée à un humain sans signal humain explicite → refusée, le
  ticket ne bouge pas. L'agent ne valide jamais (gates, recette, prod = humains).
- **Scrutateur JIRA** (workflow `jira-watch`, `workflows/jira_watch.py`) :
  passage court planifié dans Studio (`python scripts/schedule_watch.py`, défaut
  toutes les 10 min lun-ven 8h-19h Europe/Paris). JQL `jira_watch_jql` (défaut :
  `A faire` + étiquette `agent`). Pour chaque ticket : prise « A faire » →
  « Pris en charge » sous la garde v4 (by_human=False) AVANT de lancer
  `spec-to-tests` en enfant détaché — le ticket sort de la JQL, jamais deux
  pipelines sur le même ticket. Repo d'après les étiquettes (`micro` /
  `microservice` / `c8y`, sinon `jira_watch_default_repo`), résolu via
  `gitlab_<alias>_repo`. Au plus `max_starts` (2) lancements par passage.
- **Jamais de credential JIRA dans le code, les logs ni une URL** : résolution
  par la plateforme selon l'identité du run — credential utilisateur en local
  (`mistral dev`) ; **credential `atlassian` au scope workspace requis** pour
  les déploiements managés (runs non attended, service account du worker).
  Le slot est déclaré `run_as="deployment", auto_auth=False` avec
  `credentials_name` épinglé (env `jira_credentials_name`, défaut `atlassian`) :
  le preflight `auto_auth` du SDK liste les credentials au scope **user**
  (`GET /v1/connectors/<id>/user/credentials`) et reçoit un 404 « No consumer
  found ... type user » quand il est appelé par le service account d'un
  déploiement managé, ce qui faisait échouer l'exécution entière avant la
  première activité (exécution `0138f8a4...860559` / run
  `01a1eef1-1481-701a-bf52-fead246a72ec`). Avec `auto_auth=False`, les appels
  d'outils passent par la gateway du connecteur, qui résout côté serveur le
  credential épinglé par `credentials_name` si `jira_credentials_name` est
  défini, sinon le credential par défaut du caller (user, puis workspace, puis
  organisation). Nom/ID du connecteur : env `jira_connector_name` (défaut
  `01a1ef7f-0488-7652-934c-83b2eca95331` = connecteur workspace `atlassian_94e0`,
  serveur `https://mcp.atlassian.com/v2/mcp`, auth au niveau du connecteur donc
  utilisable par le service account d'un déploiement managé).
  Quand aucun credential n'est utilisable sous l'identité du run (connecteur
  OAuth user-only en déploiement managé), l'appel d'outil renvoie 401
  « Authentication failed against MCP server while calling tool » (exécution
  `0138f8a4...66ea` / run `01a1ef07-2634-701c-9223-1320f1e38a36`) :
  `fetch_jira_issue` retourne alors `{"ok": False, "reason":
  "connector_unavailable"}` et le run continue sans le ticket — JIRA ne doit
  jamais tuer le pipeline (commentaires/transitions déjà non bloquants) ; un
  ticket introuvable (mauvaise clé) reste fatal.
  Même traitement quand la gateway répond 200 mais que le tool MCP renvoie une
  erreur d'auth dans son contenu (`ConnectorToolCallError`, réponse dans
  `details`, invisible dans `str(exc)`) — ex. « You don't have permission to
  connect via API token. Please ask your organization admin for access. »
  (connecteur `atlassian_94e0` authentifié par API token refusé par l'org ;
  exécution `0138f8a4...767f50` / run `01a1ef65-f20c-728a-957f-da137a617e40`) :
  `_is_connector_unavailable` inspecte `str(exc)` ET le texte des `details`.
  Même traitement pour une erreur de scopes insuffisants renvoyée dans le contenu
  du tool (403 « Insufficient scopes for "getJiraIssue". Required:
  [read:jira:agent-interface] » — credential résolu sur le connecteur
  `atlassian_49bb` sans le scope demandé ; exécution `0138f8a4...c2740` / run
  `01a1ef8e-f451-75d8-b273-e66aae99cec2`) : marqueur « insufficient scope » dans
  `_CONNECTOR_UNAVAILABLE_MARKERS`. Une 403 au niveau du ticket (mauvaise clé)
  reste fatale.
- Env (minuscules, fallback majuscules) : `jira_base_url` (défaut
  `https://toollive.atlassian.net/jira`), `jira_cloud_id` (UUID ou host du site),
  `jira_transition_start` (défaut « Pris en charge » ; alias `jira_status_start`), `jira_transition_review`
  (défaut « code-review »), `jira_transition_done` (défaut vide = aucune),
  `jira_credentials_name` (nom du credential épinglé par le slot connecteur ;
  vide par défaut = credential par défaut du caller), `jira_connector_name`
  (ID/nom du connecteur MCP, défaut `01a1ef49-…` = `atlassian_94e0`).
- `scripts/jira.py` = recherche/lecture de tickets côté client (ton credential :
  `python scripts/jira.py 'project = TOOLLIVE AND created >= -14d ORDER BY created DESC'`).
- Tests : `uv run --with pytest python -m pytest tests/ -q`.

## TODO à compléter

- Binder le canal de notification concret (Slack incoming webhook) en secret
  `approval_webhook_url` du déploiement worker.

Convention : toutes les variables d'environnement du pipeline sont en
minuscules (`gitlab_token`, `approval_webhook_url`, `pipeline_workspace`, ...)
sauf `MISTRAL_API_KEY` (nom réservé, injecté en majuscules par le runtime
managé — lu en fallback de `mistral_api_key`).
- Déployer le worker : miroir GitHub public + `create_deployment` (managé,
  `deploy/docker/Dockerfile.worker`) ou CI GitLab + Helm chart (self-hosted).
  Les managed deployments ne buildent que depuis GitHub public pour l'instant.

## Démarrage

```bash
export mistral_api_key=...               # (fallback: MISTRAL_API_KEY en majuscules)
uv sync                                   # installe le worker
mistral dev                               # terminal 1 : le worker attend
python scripts/run.py                     # terminal 2 : lance une exécution
```

Déploiement : managé via un miroir GitHub public du repo (pas de clé API dans
le repo ; les managed deployments ne supportent que GitHub public pour
l'instant) ou self-hosted (CI GitLab + Helm chart). Voir `docs/architecture.md`.
Docs : https://docs.mistral.ai/studio/workflows/overview
