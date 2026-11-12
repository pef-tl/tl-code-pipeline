"""Activities Git : clone du repo cible (URL GitLab/GitHub ou chemin local),
worktrees isolés, commits, fusions séquentielles, push et Merge Requests.

Chaque activité est durable : retry/backoff configurés par le décorateur,
état préservé en cas de crash du worker.

Le token d'accès au forge (GitLab...) vient de l'env `gitlab_token` (ou
`pipeline_git_token`). Le worker n'a PAS de clé SSH (conteneur managé) :
les URL SSH (`git@host:...`, `ssh://...`) sont normalisées en HTTPS et le
token est envoyé en Basic auth via http.extraHeader (doc GitLab : n'importe
quel username non vide + PAT en mot de passe) — jamais dans une URL loggée.
L'API REST GitLab, elle, prend le token en `Authorization: Bearer`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

import mistralai.workflows as workflows


async def _sh(cmd: list[str], cwd: str, extra_env: dict | None = None) -> dict:
    """Exécute une commande shell, renvoie un résultat JSON-sérialisable."""
    env = {**os.environ, **(extra_env or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    return {"code": proc.returncode, "output": out.decode(errors="replace")}


def git_token() -> str | None:
    return os.environ.get("gitlab_token") or os.environ.get("pipeline_git_token")


def _auth_header() -> str:
    """Header d'auth git over HTTPS : Basic avec le PAT en mot de passe et
    `oauth2` comme username (doc GitLab : tout username non vide convient)."""
    token = git_token()
    cred = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return f"Authorization: Basic {cred}"


def _auth_env() -> dict:
    """Env git transient (GIT_CONFIG_*) pour authentifier clone/fetch/push
    sans mettre le token dans l'URL ni dans la ligne de commande."""
    if not git_token():
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": _auth_header(),
    }


def is_url(repo: str) -> bool:
    return bool(re.match(r"^(?:https?|ssh)://|^[^@/\s]+@[^:/\s]+:", repo))


def normalize_repo_url(repo: str) -> str:
    """Normalise une URL git en HTTPS (le worker n'a pas de clé SSH) :
    `git@host:group/projet.git` / `ssh://git@host[:port]/group/projet.git`
    -> `https://host/group/projet.git`. Les http(s) passent telles quelles."""
    if repo.startswith(("http://", "https://")):
        return repo
    m = re.match(r"^(?:ssh://)?[^@/\s]+@([^:/\s]+)(?::\d+)?[/:](.+)$", repo)
    if m:
        host, path = m.group(1), m.group(2)
        return f"https://{host}/{path}"
    return repo


async def base_branch(repo: str) -> str:
    """Branche de base du repo cible : env `pipeline_base_branch`, sinon la
    première de (develop, main, master) qui existe — convention toollive :
    `develop` (les deux repos ont aussi main/master, mais on merge sur develop)."""
    env = os.environ.get("pipeline_base_branch")
    if env:
        return env
    for candidate in ("develop", "main", "master"):
        res = await _sh(["git", "rev-parse", "--verify", "--quiet", candidate], repo)
        if res["code"] == 0:
            return candidate
    return "HEAD"


@workflows.activity()
async def ensure_workspace(repo: str) -> dict:
    """Si `repo` est une URL (GitLab/GitHub), clone le repo dans un workspace
    local (`pipeline_workspace`, défaut /tmp/pipeline-workspaces) et retourne le
    chemin. Si c'est un chemin local, le retourne tel quel (mode local)."""
    if not is_url(repo):
        return {"path": repo, "url": None}
    repo = normalize_repo_url(repo)   # SSH -> HTTPS (pas de clé SSH dans le worker)
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    digest = hashlib.md5(repo.encode()).hexdigest()[:8]
    root = os.environ.get("pipeline_workspace",
                          os.path.join(tempfile.gettempdir(), "pipeline-workspaces"))
    dest = os.path.join(root, f"{name}-{digest}")
    if os.path.isdir(os.path.join(dest, ".git")):
        await _sh(["git", "fetch", "origin"], dest, extra_env=_auth_env())
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
        await _sh(["git", "reset", "--hard", "origin/" + await base_branch(dest)], dest)
    else:
        os.makedirs(root, exist_ok=True)
        res = await _sh(["git", "clone", repo, dest], root, extra_env=_auth_env())
        if res["code"] != 0:
            raise RuntimeError(f"clone failed: {res['output']}")
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
    # Le token persiste dans .git/config du workspace (pas dans le remote URL) :
    if git_token():
        await _sh(["git", "config", "http.extraHeader", _auth_header()], dest)
    return {"path": dest, "url": repo}


@workflows.activity()
async def create_worktree(repo: str, task_id: str) -> dict:
    """Isole une tâche dans `../worktrees/<nom-du-repo>/<task_id>` (chemin
    absolu retourné) sur une branche dédiée `agent/<task_id>`."""
    branch = f"agent/{task_id}"
    rel = f"../worktrees/{os.path.basename(os.path.normpath(repo))}/{task_id}"
    path = os.path.abspath(os.path.join(repo, rel))
    res = await _sh(["git", "worktree", "add", "-b", branch, path], repo)
    if res["code"] != 0:
        raise RuntimeError(f"worktree failed: {res['output']}")
    return {"repo": repo, "worktree": path, "branch": branch}


@workflows.activity()
async def commit_all(worktree: str, message: str) -> dict:
    res = await _sh(["bash", "-lc", "git add -A && git commit -m \"$1\"", "_", message], worktree)
    return res


@workflows.activity()
async def merge_branch(repo: str, branch: str) -> dict:
    """Fusion séquentielle dans la branche courante — appelée une branche à la fois."""
    res = await _sh(["git", "merge", "--no-ff", branch, "-m", f"merge {branch}"], repo)
    if res["code"] != 0:
        await _sh(["git", "merge", "--abort"], repo)
        raise RuntimeError(f"merge conflict on {branch}: {res['output']}")
    return res


@workflows.activity()
async def create_branch(repo: str, branch: str) -> dict:
    return await _sh(["git", "checkout", "-b", branch], repo)


@workflows.activity()
async def push_branch(repo: str, branch: str) -> dict:
    """Push la branche courante du repo vers origin (token via .git/config)."""
    res = await _sh(["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"], repo)
    if res["code"] != 0:
        raise RuntimeError(f"push {branch} failed: {res['output']}")
    return res


@workflows.activity()
async def diff_against_base(worktree: str) -> dict:
    """Diff de la branche courante contre la branche de base détectée."""
    base = await base_branch(worktree)
    res = await _sh(["git", "diff", f"{base}...HEAD"], worktree)
    return {"diff": res["output"], "base": base}


@workflows.activity()
async def cleanup_worktree(repo: str, worktree: str, branch: str) -> dict:
    await _sh(["git", "worktree", "remove", "--force", worktree], repo)
    return await _sh(["git", "branch", "-D", branch], repo)


# ------------------------------------------------------------- API GitLab ---

def _gitlab_api(repo_url: str) -> tuple[str, str]:
    """(api_base, project encodé) à partir d'une URL git (SSH normalisée en HTTPS).
    https://gitlab.com/<group>/<projet>.git -> https://gitlab.com/api/v4, <group>%2F<projet>"""
    repo_url = normalize_repo_url(repo_url)
    m = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?/?$", repo_url)
    if not m:
        raise ValueError(f"URL git non reconnue: {repo_url}")
    host, path = m.group(1), m.group(2)
    return f"https://{host}/api/v4", urllib.parse.quote(path, safe="")


async def _gitlab_request(method: str, url: str, body: dict | None = None) -> dict:
    token = git_token()
    if not token:
        return {"error": "GITLAB_TOKEN non défini"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with await asyncio.to_thread(urllib.request.urlopen, req, 30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"}


@workflows.activity()
async def open_merge_request(repo_url: str, source_branch: str, target_branch: str,
                             title: str, description: str = "") -> dict:
    """Ouvre une MR sur GitLab (la gate de review humaine devient la review MR)."""
    if not is_url(repo_url):
        return {"mr": None, "reason": "repo local (pas d'URL)"}
    api, project = _gitlab_api(repo_url)
    mr = await _gitlab_request(
        "POST", f"{api}/projects/{project}/merge_requests",
        {"source_branch": source_branch, "target_branch": target_branch,
         "title": title, "description": description, "remove_source_branch": True},
    )
    return {"mr": mr if "iid" in mr else None, "raw": mr}


@workflows.activity()
async def merge_merge_request(repo_url: str, iid: int) -> dict:
    """Merge la MR après approbation humaine (gate 2)."""
    api, project = _gitlab_api(repo_url)
    return await _gitlab_request(
        "PUT", f"{api}/projects/{project}/merge_requests/{iid}/merge",
        {"should_remove_source_branch": True},
    )
