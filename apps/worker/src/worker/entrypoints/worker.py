"""Worker entrypoint — découvre et enregistre tous les workflows du package."""

import os

import mistralai.workflows as workflows

import worker.workflows  # noqa: F401  (provoque l'import des définitions)


async def main() -> None:
    # `mistral_api_key` en priorité ; fallback sur MISTRAL_API_KEY (nom réservé
    # injecté en majuscules par le runtime managé Mistral Cloud).
    api_key = os.environ.get("mistral_api_key") or os.environ.get("MISTRAL_API_KEY")

    # Diagnostic boot (sans jamais logger de valeur) : quels credentials sont présents
    present = {k: bool(os.environ.get(k) or os.environ.get(k.upper()))
               for k in ("gitlab_token", "approval_webhook_url",
                         "gitlab_c8y_repo", "gitlab_micro_repo", "pipeline_workspace")}
    print(f"worker boot — credentials présents: {present}", flush=True)

    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    await workflows.run_worker(workflow_classes, api_key=api_key)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
