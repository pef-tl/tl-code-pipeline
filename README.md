# code-pipeline — Pipeline multi-agents Mistral Workflows (spec → dev → tests)

Pipeline autonome de développement : un agent de spec (basé sur les skills
[OpenSpec](https://openspec.dev/)) interviewe l'humain et produit un change
proposal, un agent planner décompose en tâches, des agents dev implémentent en
parallèle dans des git worktrees isolés, un agent tester fait tourner la boucle
dev→tests, et chaque étape sensible passe par une gate humaine (signal durable).

## Architecture

```
Humain ──► agent Spec (OpenSpec) ──► GATE: approbation du proposal
              │
              ▼
        agent Planner (parse tasks.md)
              │
              ▼ (parallèle, 1 worktree par tâche)
        agents Dev ──► activity run_tests ──┐
              ▲                             │ échec (borné à N tours)
              └─────────────────────────────┘
              │
              ▼
        fusions séquentielles + re-run complet
              │
              ▼
        GATE: review du diff ──► merge final
```

## Démarrage

```bash
# 1. Prérequis
#    - clé API exportée: export mistral_api_key=...   (fallback: MISTRAL_API_KEY)
#    - Mistral CLI: https://docs.mistral.ai/studio/mistral-cli/quickstart
#    - uv, git

# 2. Installer les dépendances du worker
uv add 'mistralai-workflows[mistralai]'

# 3. Initialiser OpenSpec dans le repo CIBLE (celui que le pipeline modifie)
cd /chemin/vers/ton-repo
openspec init          # installe AGENTS.md + skills openspec-* (propose, apply, verify...)

# 4. Lancer le worker
mistral dev

# 5. Lancer une exécution
python scripts/run.py "Ajouter ..." --repo c8y     # front Angular toollive-c8y
python scripts/run.py "Ajouter ..." --repo micro   # microservice Python
# mode cloud (repos cibles sur GitLab) :
#   export gitlab_c8y_repo=https://gitlab.com/<org>/toollive-c8y.git
#   python scripts/run.py "Ajouter ..." --repo c8y

# 6. Interagir avec le workflow en cours (notification webhook avec l'execution_id) :
python scripts/status.py <execution_id>                       # état + transcript de l'agent
python scripts/talk.py <execution_id> "oui, mais sans migration"  # répondre à l'agent spec
python scripts/approve.py <execution_id> approve               # valider une gate (ou reject)
```

En mode cloud, le pipeline clone le repo cible GitLab, ouvre une Merge Request
`release/<change_id>` (review finale dans GitLab) et la merge après `approve`.
Déploiement du worker : managé (miroir GitHub, `deploy/docker/Dockerfile.worker`)
ou self-hosted (CI GitLab + Helm chart) — voir `docs/architecture.md`.

## Repos cibles

Deux repos sont configurés (détail dans `docs/architecture.md`) :

- **Front Angular** `C:\projets\toollive-c8y` (alias `c8y`) — Angular 18 +
  Cumulocity, OpenSpec déjà initialisé, tests Jest auto-détectés
  (`npm run test-unit -- --ci`), lint `npm run lint`, branche de base `develop`.
- **Microservice Python** `C:\projets\toollive-c8y-microservice` (alias `micro`) —
  Python 3.14 + Flask + uv, OpenSpec initialisé, tests `uv run pytest -x -q`,
  lint `uv run ruff check`, branche de base `develop`.

La commande de test est auto-détectée selon le repo (override :
`pipeline_test_cmd`), la branche de base aussi (override :
`pipeline_base_branch`). Toutes les variables d'env du pipeline sont en
minuscules.

## Points clés

- **Durabilité** : chaque activité est enregistrée ; un worker qui crashe reprend
  au dernier pas complété (Temporal sous le capot).
- **Gates humaines** : `wait_for_approval` met le workflow en sommeil sur un
  signal — aucune ressource consommée pendant l'attente.
- **Boucle bornée** : `run_dev_agent` ⇄ `run_tests`, max `MAX_DEV_ITERATIONS`
  tours, puis escalade humaine.
- **Isolation** : un git worktree par tâche ; fusions séquentielles, suite de
  tests complète re-jouée après chaque fusion.

## Fichiers

```
apps/worker/src/worker/
├── entrypoints/worker.py        # discovery des workflows
├── workflows/spec_to_tests.py   # orchestrateur durable
├── activities/
│   ├── git_tools.py             # worktree, commit, merge
│   ├── test_runner.py           # quality gate bloquante
│   └── approval.py              # gate humaine (signal)
└── agents/
    ├── definitions.py           # spec/planner/dev/tester (Agents SDK)
    └── run_agent.py             # boucle Runner + tools

openspec/                       # exemple de la structure attendue dans le repo cible
docs/architecture.md
scripts/run.py
```

## Références

- Mistral Workflows : https://docs.mistral.ai/studio/workflows/overview
- Agents SDK : https://docs.mistral.ai/studio/agents/overview
- OpenSpec skills : https://openspec.dev/docs/skills
