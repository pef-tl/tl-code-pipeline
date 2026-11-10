# Architecture — code-pipeline

Pipeline multi-agents autonome de développement, exécuté sur **Mistral
Workflows** (orchestration durable, Temporal sous le capot) :
**spec (OpenSpec) → gate humaine → plan → dev parallèle (git worktrees) →
boucle tests bornée → fusions séquentielles → gate humaine finale → merge**.

## Vue d'ensemble

```
Humain ──► agent Spec (OpenSpec) ──► GATE 1 : approbation du proposal (signal)
              │
              ▼
        agent Planner (parse tasks.md → JSON de tâches + dépendances)
              │
              ▼ (parallèle, 1 worktree par tâche indépendante)
        agents Dev ⇄ agent Tester ──► activity run_tests   (boucle bornée)
              │
              ▼
        fusions séquentielles dans la branche de base + re-run complet des tests
              │
              ▼
        GATE 2 : review du diff cumulé (signal) ──► merge final / archive
```

## Composants

```
apps/worker/src/worker/
├── entrypoints/worker.py        # discovery des workflows + run_worker
├── workflows/spec_to_tests.py   # orchestrateur durable "spec-to-tests" (6 étapes)
├── activities/
│   ├── git_tools.py             # worktree add/remove, commit, merge séquentiel, diff
│   ├── test_runner.py           # run_tests (quality gate) + gates humaines (approval)
│   └── openspec_tools.py        # CLI openspec exposé comme tools (list/read/write/validate)
└── agents/
    ├── definitions.py           # spec / planner / dev / tester / reviewer + bornes
    ├── run_agent.py             # boucle modèle → tools, bornée à MAX_AGENT_TURNS
    └── tools.py                 # shell/fichiers confinés au worktree (safe_path)

scripts/run.py                   # déclenche une exécution (--repo c8y|micro|<chemin>)
openspec/config.yaml             # contexte du pipeline (les repos cibles ont le leur)
docs/architecture.md             # ce document
```

### Agents et modèles

| Agent    | Rôle                                             | Modèle                  |
|----------|--------------------------------------------------|-------------------------|
| spec     | produit le change folder OpenSpec (jamais de code)| `mistral-large-latest`  |
| planner  | tasks.md → tâches JSON avec dépendances          | `mistral-large-latest`  |
| dev      | implémente UNE tâche dans SON worktree           | `mistral-medium-latest` |
| tester   | écrit/met à jour les tests des scénarios WHEN/THEN| `mistral-medium-latest` |
| reviewer | verdict JSON sur le diff cumulé (gate finale)    | `mistral-large-latest`  |

Un objet `Agent` ne traverse pas Temporal : seuls nom/paramètres passent ;
l'agent est reconstruit dans l'activité via `run_agent` (Agents SDK, boucle
bornée à `MAX_AGENT_TURNS = 20` tours d'outils).

## Déroulement d'une exécution

`run(repo, request)` — `repo` = chemin du repo cible, `request` = demande en
langage naturel.

1. **Spec** — l'agent spec travaille dans le repo cible (`openspec init` requis)
   et produit `openspec/changes/<id>/{proposal.md, design.md, tasks.md, specs/}`.
   Chaque requirement a au moins un scénario WHEN/THEN testable.
2. **GATE 1** — `open_approval` notifie l'humain, `wait_for_approval` dort sur
   le signal `approve`/`reject` (durable, aucun worker actif pendant l'attente).
3. **Planner** — décompose en tâches `{id, title, capability, files_hint,
   depends_on, acceptance}` ; les interfaces partagées sont dans la première tâche.
4. **Dev parallèle** — les tâches sans dépendances tournent en parallèle
   (`workflows.workflow.parallel`), chacune dans son worktree. Boucle bornée :
   dev → commit → tester → `run_tests` ; `MAX_DEV_ITERATIONS = 5` tours max,
   sinon escalade humaine (jamais de boucle infinie).
5. **Fusions séquentielles** — une branche `agent/<task_id>` à la fois, et la
   suite de tests complète est re-jouée après CHAQUE fusion. Conflit → abort +
   escalade.
6. **GATE 2** — review du diff cumulé, signal `approve`/`reject`, puis merge.

## Garanties

- **Durabilité** : chaque activité est enregistrée ; un worker qui crashe
  reprend au dernier pas complété.
- **Isolation** : 1 tâche = 1 worktree = 1 branche ; aucun agent ne commit sur
  la branche de base. Les tools shell/fichiers des agents dev sont confinés au
  worktree (`safe_path` bloque tout path traversal, `sudo`, `rm -rf /`,
  `git push`).
- **Bornes** : `MAX_DEV_ITERATIONS = 5`, `MAX_AGENT_TURNS = 20`.
- **Pas de hors-scope** : un agent dev n'implémente que sa tâche ; le reviewer
  rejette tout hunk hors `files_hint`.

## Repos cibles configurés

Le pipeline détecte automatiquement la stack du repo passé en entrée :

| Cible | Repo | Stack | Tests (auto-détectés) | Lint | Branche de base |
|-------|------|-------|-----------------------|------|-----------------|
| `c8y` | `C:\projets\toollive-c8y` | Angular 18 + Cumulocity (`@c8y/*` 1021.x) | `npm run test-unit -- --ci --silent` (Jest, jest-preset-angular) | `npm run lint` (eslint) | `develop` |
| `micro` | `C:\projets\toollive-c8y-microservice` | Python 3.14 + Flask + uv | `uv run pytest -x -q` | `uv run ruff check` | `develop` |

Détection dans `activities/test_runner.py::test_cmd_for` :
`pyproject.toml` à la racine → pytest/uv ; `package.json` → Jest/npm ;
sinon fallback `TEST_CMD`. Override : variable d'env `PIPELINE_TEST_CMD`.

Chaque repo cible a sa propre `openspec/config.yaml` avec le `context` de sa
stack (contraintes que l'agent ne peut pas deviner depuis le code) :

- `toollive-c8y/openspec/config.yaml` — Angular/Cumulocity, modules métier,
  conventions (TOOLLIVE-C8Y.md, docs/COMMENT_DOCUMENTER.md), E2E codeceptjs
  hors scope des boucles dev.
- `toollive-c8y-microservice/openspec/config.yaml` — Flask/uv, secrets via
  `./secrets` (jamais en dur), pas de modif CI dans un change.

**Worktrees** : `../worktrees/<nom-du-repo>/<task_id>` à côté du repo cible
(chemin absolu retourné par `create_worktree`), pour éviter les collisions entre
les deux repos.

**Branche de base** : `git_tools.base_branch` — env `PIPELINE_BASE_BRANCH`, sinon
la première de `develop`, `main`, `master` qui existe (convention toollive :
`develop`, même si `main`/`master` existent aussi). Le diff de review est calculé
contre cette branche.

Lancement :

```bash
python scripts/run.py "<demande>" --repo c8y      # front Angular
python scripts/run.py "<demande>" --repo micro    # microservice Python
python scripts/run.py "<demande>" --repo C:\chemin\vers\autre\repo   # mode local
python scripts/run.py "<demande>" --repo https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git  # mode cloud
```

En mode cloud, les alias `c8y` / `micro` sont résolus via les variables
d'env `GITLAB_C8Y_REPO` / `GITLAB_MICRO_REPO` (URL GitLab des repos cibles).
Le worker clone alors le repo cible dans `PIPELINE_WORKSPACE` avec
`GITLAB_TOKEN` (voir `git_tools.ensure_workspace`), pousse une branche
`release/<change_id>`, ouvre une **Merge Request GitLab** (la gate de review
finale = la review de la MR), et merge la MR après le signal `approve`.

## Gates humaines (signaux approve/reject)

Le workflow `SpecToTests` enregistre deux signaux et dort sur
`workflow.wait_condition` (aucun coût compute pendant l'attente) :

```python
@workflows.workflow.signal(name="approve")
async def on_approve(self) -> None: self.verdict = "approve"
# + "reject" ; attente : await wf.wait_condition(lambda: self.verdict is not None,
#                                               timeout=timedelta(hours=24))
```

- Notification : `test_runner.open_approval` poste sur le webhook
  `APPROVAL_WEBHOOK_URL` (Slack...) avec l'`execution_id` et la commande à taper.
- Réponse humaine : `python scripts/approve.py <execution_id> approve|reject`
  → `client.workflows.executions.signal_workflow_execution(execution_id, name, input)`.
  Équivalent REST : `POST /v1/workflows/executions/{id}/signals`.

## Déploiement du worker (cloud)

Deux modes (doc : *Managed deployments* / *Self-hosted*) :

| | Managé | Self-hosted |
|---|---|---|
| Source du build | **miroir GitHub public** du repo `code-pipeline` (GitLab non supporté pour l'instant par les managed deployments) | repo **GitLab** privé, image buildée par la CI GitLab |
| Infra | aucune — Mistral Cloud build et run (`create_deployment(name=..., spec={"github_url": ..., "backend_spec": {"type": "mistral_cloud", "dockerfile_path": "deploy/docker/Dockerfile.worker"}})`) | votre cluster K8s via le Helm chart Mistral |
| Auth worker | service account injecté (pas de clé dans le repo) | `MISTRAL_API_KEY` + `DEPLOYMENT_NAME` (env) |

Dans les deux modes : `deploy/docker/Dockerfile.worker` build l'image du
worker (`python -m worker.entrypoints.worker`), `DEPLOYMENT_NAME` sert au
routage des exécutions, `MISTRAL_API_KEY` est injectée automatiquement par le
runtime managé (nom réservé), et les autres secrets (`GITLAB_TOKEN`,
`APPROVAL_WEBHOOK_URL`, URLs des repos) sont bindés via le Secrets Manager
(managé) ou les secrets du cluster (self-hosted).

### Procédure managée (repo GitHub public `pef-tl/tl-code-pipeline`)

1. Créer les secrets dans **Studio > Secrets Manager** (workspace) :
   `gitlab_token`, `approval_webhook_url`,
   `gitlab_c8y_repo` (= `https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git`),
   `gitlab_micro_repo` (= `.../toollive-c8y-microservice.git`),
   `pipeline_workspace` (= `/tmp/pipeline-workspaces`).
   **Ne pas créer de secret `mistral_api_key`** : `MISTRAL_API_KEY` est un nom
   réservé du runtime managé, injecté automatiquement dans le worker (l'API
   refuse de le binder explicitement).
2. `export MISTRAL_API_KEY=...` puis `python scripts/deploy.py` →
   `create_deployment(name="code-pipeline", spec={"github_url": "https://github.com/pef-tl/tl-code-pipeline", "revision": "main", "backend_spec": {"type": "mistral_cloud", "dockerfile_path": "deploy/docker/Dockerfile.worker", "secrets": [...]}})`.
   Mistral Cloud clone, build et run le worker ; chaque push sur `main` redéploie.
   Attendre le statut **Active** (Studio > Deployments).
3. Lancer une exécution : `python scripts/run.py "<demande>" --repo c8y`
   (l'alias lit `GITLAB_C8Y_REPO` côté client).
4. Gate humaine : notification webhook avec l'`execution_id`, puis
   `python scripts/approve.py <execution_id> approve`.
5. Observer : Studio > Executions (history, traces), MR GitLab ouverte par le
   pipeline en fin de run.

**Les repos cibles (toollive-c8y, toollive-c8y-microservice) restent sur
GitLab** : le worker les clone via leur URL + `GITLAB_TOKEN`, pousse les
branches `agent/*` et `release/<change_id>`, ouvre/merge la MR via l'API
GitLab. Aucun changement côté repos cibles.

## Variables d'environnement

| Variable | Rôle |
|----------|------|
| `MISTRAL_API_KEY` | clé API (client scripts + appels LLM des agents dans le worker) |
| `PIPELINE_REPO` | repo cible par défaut de `scripts/run.py` (défaut: `c8y`) |
| `GITLAB_C8Y_REPO` / `GITLAB_MICRO_REPO` | URL GitLab résolues par les alias `c8y` / `micro` en mode cloud |
| `PIPELINE_TEST_CMD` | override de la commande de test |
| `PIPELINE_BASE_BRANCH` | override de la branche de base (défaut: auto-détection `develop`/`main`/`master`) |
| `GITLAB_TOKEN` (ou `PIPELINE_GIT_TOKEN`) | token d'accès GitLab des repos cibles (clone/push/MR) — scopes `read_repository`, `write_repository`, `api` |
| `APPROVAL_WEBHOOK_URL` | webhook de notification des gates (Slack incoming webhook…) |
| `PIPELINE_WORKSPACE` | dossier de clone des repos cibles en mode URL (défaut `/tmp/pipeline-workspaces`) |
| `DEPLOYMENT_NAME` | nom du déploiement worker (requis au boot ; routage des exécutions) |

## Points à brancher (TODO restants)

- Choisir le canal de notification concret (Slack incoming webhook recommandé)
  et binder `APPROVAL_WEBHOOK_URL` comme secret du déploiement.
- Déployer le worker : miroir GitHub public + `create_deployment` (managé),
  ou CI GitLab + Helm chart (self-hosted). Voir *Déploiement du worker*.

## Démarrage

```bash
export MISTRAL_API_KEY=...
uv sync                 # installe le worker
mistral dev             # terminal 1 : le worker attend
python scripts/run.py "Ajouter ..." --repo c8y   # terminal 2 : lance une exécution
```

Références : Mistral Workflows
(https://docs.mistral.ai/studio/workflows/overview), Agents SDK
(https://docs.mistral.ai/studio/agents/overview), OpenSpec
(https://openspec.dev/docs/skills).
