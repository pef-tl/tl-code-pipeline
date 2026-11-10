"""Worker entrypoint — découvre et enregistre tous les workflows du package."""

import os

import mistralai.workflows as workflows

import worker.workflows  # noqa: F401  (provoque l'import des définitions)


async def main() -> None:
    # `mistral_api_key` en priorité ; fallback sur MISTRAL_API_KEY (nom réservé
    # injecté en majuscules par le runtime managé Mistral Cloud).
    api_key = os.environ.get("mistral_api_key") or os.environ.get("MISTRAL_API_KEY")

    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    await workflows.run_worker(workflow_classes, api_key=api_key)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
