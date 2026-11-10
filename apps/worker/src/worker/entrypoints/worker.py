"""Worker entrypoint — découvre et enregistre tous les workflows du package."""

import os

from mistralai.client import Mistral

import mistralai.workflows as workflows

import worker.workflows  # noqa: F401  (provoque l'import des définitions)


async def main() -> None:
    api_key = os.environ["MISTRAL_API_KEY"]
    client = Mistral(api_key=api_key)

    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    await client.workflows.run_worker(workflow_classes)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
