"""Quality gate bloquante : la suite de tests, et la notification des gates humaines.

run_tests est la gate exécutée après CHAQUE boucle dev et après CHAQUE fusion.
open_approval notifie l'humain (webhook Slack/Linear...) qu'une décision l'attend ;
l'attente elle-même se fait CÔTÉ WORKFLOW via workflow.wait_condition sur les
signaux `approve` / `reject` (voir workflows/spec_to_tests.py) — le workflow dort
sans aucun coût compute pendant que l'humain relit.
https://docs.mistral.ai/studio/workflows/building-workflows/waiting_for_conditions
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request

import mistralai.workflows as workflows

TEST_CMD = ["pytest", "-x", "-q"]       # fallback si la stack n'est pas détectée
TEST_TIMEOUT_SECONDS = 1800             # la suite Jest d'un gros front peut être longue
DEP_TIMEOUT_SECONDS = 1800              # npm ci / uv sync sur un clone frais
REVIEW_TIMEOUT_SECONDS = 24 * 3600     # la gate humaine attend jusqu'à 24h


def test_cmd_for(worktree: str) -> list[str]:
    """Commande de test auto-détectée selon la stack du repo cible :

    - repo Python (pyproject.toml à la racine)   -> `uv run pytest -x -q`
      (ex. C:\\projets\\toollive-c8y-microservice)
    - repo Angular/Node (package.json à la racine) -> `npm run test-unit -- --ci`
      (ex. C:\\projets\\toollive-c8y, Jest via jest-preset-angular)
    - override explicite : variable d'env `pipeline_test_cmd`
    """
    override = os.environ.get("pipeline_test_cmd")
    if override:
        return shlex.split(override)
    if os.path.isfile(os.path.join(worktree, "pyproject.toml")):
        return ["uv", "run", "pytest", "-x", "-q"]
    if os.path.isfile(os.path.join(worktree, "package.json")):
        return ["npm", "run", "test-unit", "--", "--ci", "--silent"]
    return TEST_CMD


async def _run_logged(cmd: list[str], cwd: str, timeout: int) -> dict:
    if os.name == "nt" and cmd[0] in ("npm", "npx", "ng", "uv"):
        cmd = ["cmd", "/c", *cmd]      # npm/ng/uv sont des .cmd sous Windows
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return {"passed": proc.returncode == 0, "cmd": " ".join(cmd),
            "log": out.decode(errors="replace")}


async def ensure_deps(worktree: str) -> dict:
    """Installe les dépendances d'un clone frais (le worker clone à vide) :
    - package.json sans node_modules -> `npm ci` (fallback `npm install`)
    - pyproject.toml sans .venv      -> `uv sync`
    Helper interne (appelé par run_tests), pas une activité séparée.
    """
    logs = []
    if (os.path.isfile(os.path.join(worktree, "package.json"))
            and not os.path.isdir(os.path.join(worktree, "node_modules"))):
        res = await _run_logged(["npm", "ci", "--no-audit", "--no-fund"],
                                worktree, DEP_TIMEOUT_SECONDS)
        if not res["passed"]:    # lockfile absent ou incompatible -> install
            res2 = await _run_logged(["npm", "install", "--no-audit", "--no-fund"],
                                     worktree, DEP_TIMEOUT_SECONDS)
            res = res2 if res2["passed"] else res
        logs.append(res["log"][-2000:])
    if (os.path.isfile(os.path.join(worktree, "pyproject.toml"))
            and not os.path.isdir(os.path.join(worktree, ".venv"))):
        res = await _run_logged(["uv", "sync"], worktree, DEP_TIMEOUT_SECONDS)
        logs.append(res["log"][-2000:])
    return {"log": "\n".join(logs)}


@workflows.activity()
async def run_tests(worktree: str) -> dict:
    deps = await ensure_deps(worktree)
    result = await _run_logged(test_cmd_for(worktree), worktree, TEST_TIMEOUT_SECONDS)
    result["log"] = (deps["log"] + "\n" + result["log"]) if deps["log"] else result["log"]
    return result


@workflows.activity()
async def open_approval(subject: str, payload: str, execution_id: str = "") -> dict:
    """Notifie l'humain qu'une gate attend sa décision (signal approve/reject).

    Canal : webhook générique (Slack incoming webhook, Linear, ...) lu dans
    `approval_webhook_url`. Le message contient l'execution_id et la commande
    pour répondre : `python scripts/approve.py <execution_id> approve|reject`.
    """
    url = os.environ.get("approval_webhook_url")
    base = {"subject": subject, "execution_id": execution_id}
    if not url:
        return {**base, "notified": False,
                "reason": "approval_webhook_url non défini — notification ignorée "
                          "(pense à envoyer le signal à la main)"}
    text = (
        f":bell: *Gate d'approbation — {subject}*\n"
        f"Exécution : `{execution_id}`\n"
        f"Répondre : `python scripts/approve.py {execution_id} approve` "
        f"(ou `reject`)\n"
        f"```{payload[:2500]}```"
    )

    def _post() -> int:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status

    try:
        status = await asyncio.to_thread(_post)
        return {**base, "notified": True, "status": status}
    except Exception as exc:  # le workflow continue même si la notif échoue
        return {**base, "notified": False, "error": str(exc)}
