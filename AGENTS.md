# AGENTS.md — Conventions du projet code-pipeline

Ce fichier est lu automatiquement par les agents (Vibe Code CLI, VS Code
extension, agents du pipeline). Il condense toutes les décisions prises
lors de la conception du projet — respecte-les sans les rediscuter.

## Objet

Pipeline multi-agents autonome de développement, exécuté sur **Mistral
Workflows** (orchestration durable Temporal) :
**spec (OpenSpec) → gate humaine → plan → dev parallèle (git worktrees) →
boucle tests bornée → fusions séquentielles → gate humaine finale → merge**.

## Stack et dépendances

- Python 3.12, `mistralai-workflows[mistralai]` (plugin Mistral).
- Agents exécutés via l'**Agents SDK** dans des activités : un objet `Agent`
  ne traverse pas Temporal, seul son nom/paramètres passent. Ne PAS utiliser
  les durable agents (`Agent`/`Runner` du plugin) : dépréciés.
- Routing modèles : `mistral-large-latest` pour le raisonnement
  (spec/planner/reviewer), `mistral-medium-latest` pour les boucles dev/tests.
- Repo cible : **OpenSpec** initialisé (`openspec init`). La spec n'est jamais
  du texte libre : toujours un change folder conforme
  `openspec/changes/<id>/{proposal.md, design.md, tasks.md, specs/}`.

## Règles d'architecture (non négociables)

1. **Spec vérifiable** : chaque requirement a au moins un scénario WHEN/THEN
   testable. Pas de scénario, pas de requirement.
2. **Isolation** : une tâche dev = un git worktree = une branche `agent/<task_id>`.
   Jamais de commit directement sur main par un agent.
3. **Fusions séquentielles** : une branche à la fois, suite de tests complète
   re-jouée après CHAQUE fusion. Conflit → abort + escalade.
4. **Boucles bornées** : `MAX_DEV_ITERATIONS = 5` (dev→tests), `MAX_AGENT_TURNS
   = 20` (boucle d'outils). Au-delà : escalade humaine, jamais de boucle infinie.
5. **Gates humaines** : (a) approbation du proposal OpenSpec, (b) review du
   diff final. Le workflow dort sur un signal (`approve`/`reject`) —
   voir Waiting for Conditions dans la doc Mistral Workflows.
6. **Interfaces d'abord** : le planner place les types/API partagés dans la
   première tâche ; les tâches parallèles codent contre ce contrat.
7. **Tools confinés** : les tools shell/fichiers des agents dev sont bornés au
   worktree (pas de path traversal, pas de `sudo`, pas de `git push`).
8. **Pas de hors-scope** : un agent dev n'implémente QUE sa tâche ; le
   reviewer rejette tout hunk hors `files_hint`.

## Repos cibles

- Front Angular : `C:\projets\toollive-c8y` (alias `c8y` dans `scripts/run.py`) —
  tests Jest auto-détectés (`npm run test-unit -- --ci`), lint `npm run lint`,
  branche de base `develop`, OpenSpec déjà initialisé.
- Microservice Python : `C:\projets\toollive-c8y-microservice` (alias `micro`) —
  tests `uv run pytest -x -q`, lint `uv run ruff check`, branche de base
  `develop`, OpenSpec initialisé.

Détail et variables d'env : `docs/architecture.md`. La commande de test est
auto-détectée par repo (`test_runner.py::test_cmd_for`, override
`PIPELINE_TEST_CMD`) ; la branche de base est auto-détectée
(`git_tools.py::base_branch`, override `PIPELINE_BASE_BRANCH`).

En mode cloud, `--repo` accepte une **URL GitLab** (résolue aussi via
`GITLAB_C8Y_REPO` / `GITLAB_MICRO_REPO`) : le worker clone le repo cible
(`git_tools.ensure_workspace`, token `GITLAB_TOKEN`), pousse
`release/<change_id>`, ouvre une **Merge Request GitLab** et la merge après
approbation. Les repos cibles restent sur GitLab, inchangés.

Gates humaines : signaux `approve`/`reject` sur le workflow
(`@workflows.workflow.signal` + `wait_condition` dans `spec_to_tests.py`) ;
réponse via `python scripts/approve.py <execution_id> approve|reject` ;
notification via `open_approval` (webhook `APPROVAL_WEBHOOK_URL`).

## TODO à compléter

- Binder le canal de notification concret (Slack incoming webhook) en secret
  `APPROVAL_WEBHOOK_URL` du déploiement worker.
- Déployer le worker : miroir GitHub public + `create_deployment` (managé,
  `deploy/docker/Dockerfile.worker`) ou CI GitLab + Helm chart (self-hosted).
  Les managed deployments ne buildent que depuis GitHub public pour l'instant.

## Démarrage

```bash
export MISTRAL_API_KEY=...
uv sync                                   # installe le worker
mistral dev                               # terminal 1 : le worker attend
python scripts/run.py                     # terminal 2 : lance une exécution
```

Déploiement : managé via un miroir GitHub public du repo (pas de clé API dans
le repo ; les managed deployments ne supportent que GitHub public pour
l'instant) ou self-hosted (CI GitLab + Helm chart). Voir `docs/architecture.md`.
Docs : https://docs.mistral.ai/studio/workflows/overview
OpenSpec : https://openspec.dev/docs/skills
