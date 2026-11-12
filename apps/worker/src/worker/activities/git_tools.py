"""Activities Git : clone du repo cible (URL GitLab/GitHub ou chemin local),
worktrees isolés, commits, fusions séquentielles, push et Merge Requests.

Chaque activité est durable : retry/backoff configurés par le décorateur,
état préservé en cas de crash du worker.

Auth forge, selon le format de l'URL du repo (utilisée telle quelle) :
- URL SSH (`git@host:...`, `ssh://...`) : clé privée injectée via le secret
  `gitlab_ssh_key` — écrite dans un fichier 0600 et passée à git via
  GIT_SSH_COMMAND / core.sshCommand (comme sur une machine de dev).
- URL HTTPS : PAT (`gitlab_token` / `pipeline_git_token`) en Basic auth via
  http.extraHeader (doc GitLab : tout username non vide + PAT en mot de passe).
- API REST GitLab (Merge Requests) : même PAT en `Authorization: Bearer`.
Dans tous les cas : jamais de credential dans une URL loggée, et
GIT_TERMINAL_PROMPT=0 (pas de prompt interactif dans le conteneur).
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


def _env(*names: str) -> str | None:
    """Première variable d'env non vide parmi `names` — convention minuscule,
    avec fallback sur l'ancien nom en majuscules (tolère les déploiements
    créés avant la convention minuscules)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


def git_token() -> str | None:
    return _env("gitlab_token", "GITLAB_TOKEN", "pipeline_git_token", "PIPELINE_GIT_TOKEN")


def _auth_header() -> str:
    """Header d'auth git over HTTPS : Basic avec le PAT en mot de passe et
    `oauth2` comme username (doc GitLab : tout username non vide convient)."""
    token = git_token()
    cred = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return f"Authorization: Basic {cred}"


def _ssh_env() -> dict:
    """GIT_SSH_COMMAND pour le SSH : toujours `StrictHostKeyChecking=accept-new`
    + UserKnownHostsFile=/dev/null (jamais de blocage sur la clé d'hôte dans le
    conteneur) ; `-i <keyfile>` si une clé privée est fournie via le secret
    `gitlab_ssh_key` (écrite 0600, jamais dans une URL loggée)."""
    opts = ["-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={os.devnull}"]
    key = _env("gitlab_ssh_key", "GITLAB_SSH_KEY")
    if key:
        keyfile = os.path.join(tempfile.gettempdir(), "pipeline-git-ssh-key")
        if not os.path.exists(keyfile):
            with open(keyfile, "w", encoding="utf-8") as fh:
                fh.write(key if key.endswith("\n") else key + "\n")
            os.chmod(keyfile, 0o600)
        opts = ["-i", keyfile, *opts]
    return {"GIT_SSH_COMMAND": "ssh " + " ".join(opts)}


def _auth_env() -> dict:
    """Env git transient pour clone/fetch/push : GIT_TERMINAL_PROMPT=0 (jamais
    de prompt dans le conteneur), GIT_SSH_COMMAND (accept-new + clé du secret
    gitlab_ssh_key si présente) et http.extraHeader Basic PAT pour les URL https."""
    env = {"GIT_TERMINAL_PROMPT": "0", **_ssh_env()}
    if git_token():
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": _auth_header(),
        })
    return env


def is_url(repo: str) -> bool:
    return bool(re.match(r"^(?:https?|ssh)://|^[^@/\s]+@[^:/\s]+:", repo))


def _parse_repo_url(repo: str) -> tuple[str, str]:
    """(host, path) depuis une URL git quelconque :
    https://host/group/projet.git, ssh://git@host/group/projet.git,
    git@host:group/projet.git (scp-like)."""
    m = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", repo)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(?:ssh://[^@/\s]+@|[^@/\s]+@)([^:/\s]+)(?::\d+)?[/:](.+?)(?:\.git)?/?$", repo)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"URL git non reconnue: {repo}")


async def base_branch(repo: str) -> str:
    """Branche de base du repo cible : env `pipeline_base_branch`, sinon la
    première de (develop, main, master) qui existe — convention toollive :
    `develop` (les deux repos ont aussi main/master, mais on merge sur develop)."""
    env = _env("pipeline_base_branch", "PIPELINE_BASE_BRANCH")
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
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    digest = hashlib.md5(repo.encode()).hexdigest()[:8]
    root = _env("pipeline_workspace", "PIPELINE_WORKSPACE") \
        or os.path.join(tempfile.gettempdir(), "pipeline-workspaces")
    dest = os.path.join(root, f"{name}-{digest}")
    if os.path.isdir(os.path.join(dest, ".git")):
        await _sh(["git", "fetch", "origin"], dest, extra_env=_auth_env())
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
        await _sh(["git", "reset", "--hard", "origin/" + await base_branch(dest)], dest)
    else:
        os.makedirs(root, exist_ok=True)
        res = await _sh(["git", "clone", repo, dest], root, extra_env=_auth_env())
        if res["code"] != 0:
            out = res["output"]
            hints = []
            if "Host key verification failed" in out:
                hints.append("clé d'hôte SSH refusée — inattendu (accept-new), "
                             "vérifier GIT_SSH_COMMAND dans le worker")
            if "Permission denied (publickey)" in out:
                hints.append("auth SSH refusée : secret gitlab_ssh_key absent/non bindé "
                             "OU clé publique pas enregistrée comme deploy key sur le projet "
                             "(re-run python scripts/deploy.py pour binder le secret)")
            if "could not read Username" in out or "Authentication failed" in out:
                hints.append("gitlab_token absent du worker ou PAT invalide — vérifier le "
                             "binding du secret (re-run python scripts/deploy.py), scopes "
                             "read_repository/write_repository/api")
            if not git_token() and not _env("gitlab_ssh_key", "GITLAB_SSH_KEY"):
                hints.append("AUCUN credential dans le worker (ni gitlab_ssh_key ni "
                             "gitlab_token) : re-run python scripts/deploy.py après avoir "
                             "créé les secrets dans Studio > Secrets Manager")
            raise RuntimeError(f"clone failed: {out}{' — ' + ' | '.join(hints) if hints else ''}")
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
    # Les credentials persistent dans .git/config du workspace (jamais dans le
    # remote URL) : Basic PAT pour https, core.sshCommand pour le SSH.
    if git_token():
        await _sh(["git", "config", "http.extraHeader", _auth_header()], dest)
    ssh_cmd = _ssh_env().get("GIT_SSH_COMMAND")
    if ssh_cmd:
        await _sh(["git", "config", "core.sshCommand", ssh_cmd], dest)
    return {"path": dest, "url": repo, "token_present": bool(git_token()),
            "ssh_key_present": bool(ssh_cmd)}


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
    """Push la branche courante du repo vers origin (auth via .git/config
    et/ou env transient _auth_env)."""
    res = await _sh(["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"],
                    repo, extra_env=_auth_env())
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
    """(api_base, project encodé) à partir d'une URL git quelconque (https, ssh,
    scp-like git@host:path). https://gitlab.com/<group>/<projet>.git ->
    https://gitlab.com/api/v4, <group>%2F<projet>"""
    host, path = _parse_repo_url(repo_url)
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
