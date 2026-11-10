"""Tools des agents dev/tester : shell et fichiers bornés au worktree."""

from __future__ import annotations

import asyncio
import os

async def _sh(cmd: str, cwd: str) -> dict:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    return {"code": proc.returncode, "output": out.decode(errors="replace")[:8000]}


def build_dev_tools(worktree: str) -> dict:
    """(schéma, implémentation) pour run_agent — tout est confiné au worktree."""

    def safe_path(rel: str) -> str:
        full = os.path.realpath(os.path.join(worktree, rel))
        root = os.path.realpath(worktree)
        if not full.startswith(root):
            raise PermissionError(f"path escapes worktree: {rel}")
        return full

    async def shell(command: str) -> dict:
        if any(b in command for b in ("sudo ", "rm -rf /", "git push")):
            return {"error": "command not allowed"}
        return await _sh(command, worktree)

    async def read(path: str) -> dict:
        with open(safe_path(path), encoding="utf-8") as fh:
            return {"content": fh.read()[:20000]}

    async def write(path: str, content: str) -> dict:
        full = safe_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"written": path}

    return {
        "shell": ({"description": "Run a shell command inside your worktree.",
                   "parameters": {"type": "object",
                                  "properties": {"command": {"type": "string"}},
                                  "required": ["command"]}},
                  shell),
        "read_file": ({"description": "Read a file from your worktree.",
                       "parameters": {"type": "object",
                                      "properties": {"path": {"type": "string"}},
                                      "required": ["path"]}},
                      read),
        "write_file": ({"description": "Create or overwrite a file in your worktree.",
                        "parameters": {"type": "object",
                                       "properties": {"path": {"type": "string"},
                                                      "content": {"type": "string"}},
                                       "required": ["path", "content"]}},
                       write),
    }
