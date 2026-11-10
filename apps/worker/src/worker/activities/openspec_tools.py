"""Activities propres à l'agent Spec : skills OpenSpec exposés comme tools.

L'agent Spec ne "connaît" pas OpenSpec par magie : on lui donne les skills
openspec-* (https://openspec.dev/docs/skills) comme outils concrets, exécutés
via le CLI `openspec` dans le repo cible (initialisé avec `openspec init`).
"""

from __future__ import annotations

import asyncio

import mistralai.workflows as workflows


async def _openspec(args: list[str], repo: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "openspec", *args, cwd=repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    return {"code": proc.returncode, "output": out.decode(errors="replace")}


# ---- Déclarations de tools (schéma passé au modèle + implémentation) ------

OPENSPEC_TOOLS = {
    "openspec_list_specs": (
        {
            "description": "List existing capabilities. ALWAYS call this first; "
                           "reuse an existing capability name when one fits.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        lambda repo: _openspec(["list", "--specs"], repo),
    ),
    "openspec_read": (
        {
            "description": "Read a file (AGENTS.md, config.yaml, existing specs, "
                           "a change folder artifact).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        None,  # implémenté via read_file_activity
    ),
    "openspec_write": (
        {
            "description": "Write/change-folder artifact (proposal.md, design.md, "
                           "tasks.md, specs/<cap>/spec.md).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        None,
    ),
    "openspec_validate": (
        {
            "description": "Validate a change folder against the OpenSpec schema. "
                           "MUST return success before you finish.",
            "parameters": {
                "type": "object",
                "properties": {"change_id": {"type": "string"}},
                "required": ["change_id"],
            },
        },
        lambda repo, change_id: _openspec(["validate", change_id], repo),
    ),
}


@workflows.activity()
async def archive_change(repo: str, change_id: str) -> dict:
    """Archive le change après merge : specs/ mises à jour, change déplacé
    dans openspec/changes/archive/."""
    return await _openspec(["archive", change_id, "--yes"], repo)


@workflows.activity()
async def read_file_activity(repo: str, path: str) -> dict:
    with open(f"{repo}/{path}", encoding="utf-8") as fh:
        return {"content": fh.read()}


@workflows.activity()
async def write_file_activity(repo: str, path: str, content: str) -> dict:
    full = f"{repo}/{path}"
    import os
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    return {"written": path}


def build_spec_tools(repo: str) -> dict:
    """Lie chaque tool déclaré à son implémentation (activité ou closure)."""
    def _wrap(fn):
        async def bound(**kwargs):
            return await fn(repo=repo, **kwargs)
        return bound

    impls = {
        "openspec_list_specs": _wrap(OPENSPEC_TOOLS["openspec_list_specs"][1]),
        "openspec_read": _wrap(read_file_activity),
        "openspec_write": _wrap(write_file_activity),
        "openspec_validate": _wrap(OPENSPEC_TOOLS["openspec_validate"][1]),
    }
    return {name: (spec, impls[name]) for name, (spec, _) in OPENSPEC_TOOLS.items()}
