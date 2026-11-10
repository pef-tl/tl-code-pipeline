"""Déclenche une exécution du pipeline spec-to-tests.

Usage :
    python scripts/run.py "<demande produit>" [--repo c8y|micro|<URL ou chemin>]

Exemples :
    python scripts/run.py "Ajouter un icone de recherche dans le tl-input" --repo c8y
    python scripts/run.py "Exposer le statut OEM dans le microservice" --repo micro
    python scripts/run.py "..." --repo https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git

En mode cloud, les alias c8y/micro sont résolus via `gitlab_c8y_repo` /
`gitlab_micro_repo` (URL GitLab, ex. https://gitlab.toollive.bearstech.com/
toollive-team/toollive-business/toollive-c8y.git). Répondre aux gates avec :
    python scripts/approve.py <execution_id> approve|reject
"""

import argparse
import json
import os

from mistralai.client import Mistral

# Alias des repos cibles. En mode cloud, pointer vers les URL GitLab via les
# variables d'env `gitlab_c8y_repo` / `gitlab_micro_repo` (le worker clone alors le
# repo avec `gitlab_token`). En local, les chemins par défaut fonctionnent tels quels.
REPOS = {
    "c8y": os.environ.get("gitlab_c8y_repo", r"C:\projets\toollive-c8y"),
    "micro": os.environ.get("gitlab_micro_repo", r"C:\projets\toollive-c8y-microservice"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="demande produit, en langage naturel")
    parser.add_argument(
        "--repo",
        default=os.environ.get("pipeline_repo", "c8y"),
        help="alias 'c8y' / 'micro', ou chemin absolu du repo cible (défaut: c8y)",
    )
    args = parser.parse_args()
    repo = REPOS.get(args.repo, args.repo)

    # `mistral_api_key` en priorité ; fallback MISTRAL_API_KEY (nom réservé,
    # injecté en majuscules par le runtime managé).
    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    execution = client.workflows.execute_workflow(
        workflow_identifier="spec-to-tests",
        input={"repo": repo, "request": args.request},
        wait_for_result=False,
    )
    print(json.dumps({"execution_id": execution.execution_id, "repo": repo}, indent=2))


if __name__ == "__main__":
    main()
