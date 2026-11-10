"""Crée (ou met à jour) le déploiement managé Mistral Cloud du worker code-pipeline.

Usage :
    python scripts/deploy.py [--name code-pipeline]
                             [--repo https://github.com/pef-tl/tl-code-pipeline]
                             [--revision main]

Mistral Cloud clone le repo GitHub, build deploy/docker/Dockerfile.worker,
run le worker (auth service account, pas de clé dans le repo). Chaque push
sur la branche redéploie automatiquement.

Toutes les variables d'environnement du pipeline sont en minuscules et
portent le même nom que le secret du Secrets Manager (binding 1:1).

Prérequis — secrets créés dans Studio > Secrets Manager (workspace) :
    gitlab_token          -> token GitLab des repos cibles (read/write repo, api)
    approval_webhook_url  -> webhook de notification des gates humaines
    gitlab_c8y_repo       -> https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git
    gitlab_micro_repo     -> https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y-microservice.git
    pipeline_workspace    -> /tmp/pipeline-workspaces

Note : `MISTRAL_API_KEY` (majuscules) est un nom réservé du runtime managé —
la plateforme l'injecte automatiquement dans le worker (service account) ;
le code lit `mistral_api_key` en priorité avec fallback sur ce nom réservé.
"""

import argparse
import json
import os

from mistralai.client import Mistral

# env var du worker <- référence de secret du workspace Secrets Manager
SECRETS = [
    # Noms d'env en minuscules, identiques aux noms des secrets du workspace.
    # MISTRAL_API_KEY (majuscules) est réservée : injectée automatiquement par
    # le runtime managé, elle ne doit PAS apparaître ici (l'API la refuse).
    ("gitlab_token", "secret:workspace:gitlab_token"),
    ("approval_webhook_url", "secret:workspace:approval_webhook_url"),
    ("gitlab_c8y_repo", "secret:workspace:gitlab_c8y_repo"),
    ("gitlab_micro_repo", "secret:workspace:gitlab_micro_repo"),
    ("pipeline_workspace", "secret:workspace:pipeline_workspace"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="code-pipeline")
    parser.add_argument("--repo", default="https://github.com/pef-tl/tl-code-pipeline")
    parser.add_argument("--revision", default="main")
    args = parser.parse_args()

    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    spec = {
        "github_url": args.repo,
        "revision": args.revision,
        "backend_spec": {
            "type": "mistral_cloud",
            "dockerfile_path": "deploy/docker/Dockerfile.worker",
            "secrets": [{"env_var_name": env, "reference": ref} for env, ref in SECRETS],
        },
    }
    try:
        dep = client.workflows.deployments.create_deployment(name=args.name, spec=spec)
        action = "created"
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise
        dep = client.workflows.deployments.update_deployment(name=args.name, spec=spec)
        action = "updated"
    print(json.dumps({"action": action, "deployment": dep.name,
                      "status": getattr(dep, "status", None)}, indent=2, default=str))
    print("Suivi du build/run dans Studio > Deployments, ou en polling :")
    print(f"  python -c \"import os,json;from mistralai.client import Mistral;"
          f"c=Mistral(api_key=os.environ.get('mistral_api_key') or os.environ['MISTRAL_API_KEY']);"
          f"print(json.dumps(c.workflows.deployments.get('{args.name}').model_dump(),indent=2,default=str))\"")


if __name__ == "__main__":
    main()
