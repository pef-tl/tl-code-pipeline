"""Worker entrypoint — découvre et enregistre tous les workflows du package."""

import os

from mistralai.client import Mistral

import mistralai.workflows as workflows

import worker.workflows  # noqa: F401  (provoque l'import des définitions)


async def main() -> None:
    client = Mistral(api_key=os.environ.get("MISTRAL_API_KEY"))

    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    await client.workflows.run_worker(workflow_classes)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
