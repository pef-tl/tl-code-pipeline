"""Activities Git : clone du repo cible (URL GitLab/GitHub ou chemin local),
worktrees isolés, commits, fusions séquentielles, push et Merge Requests.

Chaque activité est durable : retry/backoff configurés par le décorateur,
état préservé en cas de crash du worker.

Auth forge — 100% PAT (`gitlab_token` / `pipeline_git_token`), pas de clé SSH :
- Opérations git (clone/push) : les URL SSH (`git@host:...`, `ssh://...`) sont
  converties en HTTPS (`normalize_repo_url`) et le PAT est envoyé en Basic
  auth via http.extraHeader (doc GitLab : tout username non vide + PAT en mot
  de passe) — jamais dans une URL loggée.
- API REST GitLab (Merge Requests) : même PAT en `Authorization: Bearer`.
- Fallback si le secret n'est pas bindé au déploiement : le PAT peut être
  embarqué dans l'URL du repo passée à l'exécution
  (`https://oauth2:<PAT>@gitlab.../....git`) — il est extrait, jamais loggué,
  et retiré de l'URL avant le clone (le clone se fait sur l'URL nettoyée +
  http.extraHeader).
Dans tous les cas : GIT_TERMINAL_PROMPT=0 (pas de prompt interactif dans le
conteneur) et hints guidés si un credential manque.
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


_URL_TOKEN: str | None = None  # PAT extrait d'une URL `https://user:pass@...` passée en entrée


def extract_url_token(repo: str) -> tuple[str, str | None]:
    """Si l'URL embarque des credentials (`https://user:pass@host/...`), renvoie
    (URL nettoyée, pass). Sinon (repo, None). Le token n'est jamais loggué :
    on clone l'URL nettoyée avec http.extraHeader."""
    m = re.match(r"^(https?://)[^@/\s]+:([^@/\s]+)@(.+)$", repo)
    if m:
        return f"{m.group(1)}{m.group(3)}", m.group(2)
    return repo, None


_TOKEN_ENV_NAMES = ("gitlab_token", "pipeline_git_token")


def _token_from_env() -> tuple[str | None, str | None]:
    """(PAT, nom de la variable d'env source) — recherche insensible à la casse :
    un déploiement peut binder le secret sous `GITLAB_TOKEN` (majuscules) au lieu du
    nom minuscules conventionnel. Le token est nettoyé (strip) : un secret copié
    depuis un terminal peut embarquer un saut de ligne final, ce qui invaliderait le
    credential Basic. La valeur n'est jamais logguée, seul le nom de la variable."""
    for key, val in os.environ.items():
        if key.lower() in _TOKEN_ENV_NAMES and val and val.strip():
            return val.strip(), key
    return None, None


def git_token() -> str | None:
    """PAT du pipeline : secret env `gitlab_token` (binding du déploiement, toute
    casse tolérée — ex. `GITLAB_TOKEN`), avec fallback sur le PAT embarqué
    dans l'URL du repo (voir extract_url_token)."""
    token, _src = _token_from_env()
    token = token or _URL_TOKEN
    return token.strip() if token else token


def git_token_source() -> str | None:
    """Nom de la variable d'env ayant fourni le PAT (diagnostic uniquement)."""
    _tok, src = _token_from_env()
    return src


def _auth_header() -> str:
    """Header d'auth git over HTTPS : Basic avec le PAT en mot de passe et
    `oauth2` comme username (doc GitLab : tout username non vide convient)."""
    token = git_token()
    cred = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return f"Authorization: Basic {cred}"


def _auth_env() -> dict:
    """Env git transient pour clone/fetch/push : GIT_TERMINAL_PROMPT=0 (jamais
    de prompt interactif dans le conteneur) et http.extraHeader Basic PAT
    si `gitlab_token` est présent (jamais de credential dans une URL)."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if git_token():
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": _auth_header(),
        })
    return env


def _auth_args() -> list[str]:
    """`-c http.extraHeader=...` à passer à chaque invocation git réseau
    (clone/fetch/push) : fiable quelle que soit la version de git embarquée
    dans l'image (la config par env GIT_CONFIG_KEY_n n'est honorée que depuis
    git 2.31 — sur une image plus ancienne elle est ignorée et le clone
    part sans credential)."""
    if not git_token():
        return []
    return ["-c", f"http.extraHeader={_auth_header()}"]


def is_url(repo: str) -> bool:
    return bool(re.match(r"^(?:https?|ssh)://|^[^@/\s]+@[^:/\s]+:", repo))


def normalize_repo_url(repo: str) -> str:
    """Convertit une URL git SSH en HTTPS pour les opérations git (le worker
    s'authentifie par PAT, pas par clé SSH) :
    `git@host:group/projet.git` / `ssh://git@host[:port]/group/projet.git`
    -> `https://host/group/projet.git`. Les http(s) passent telles quelles."""
    if repo.startswith(("http://", "https://")):
        return repo
    m = re.match(r"^(?:ssh://)?[^@/\s]+@([^:/\s]+)(?::\d+)?[/:](.+)$", repo)
    if m:
        host, path = m.group(1), m.group(2)
        return f"https://{host}/{path}"
    return repo


def _parse_repo_url(repo: str) -> tuple[str, str]:
    """(host, path) depuis une URL git quelconque :
    https://host/group/projet.git, ssh://git@host/group/projet.git,
    git@host:group/projet.git (scp-like)."""
    m = re.match(r"^https?://(?:[^@/\s]+@)?([^/]+)/(.+?)(?:\.git)?/?$", repo)
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


async def _probe_remote_auth(repo: str) -> None:
    """Sonde `git ls-remote` avec les mêmes credentials que le clone/fetch :
    échoue vite et précisément si aucun PAT n'est configuré ou si le forge
    refuse le PAT (401), au lieu d'un clone qui échoue plus tard et moins clair.
    La valeur du PAT n'est jamais incluse dans l'erreur."""
    res = await _sh(["git", *_auth_args(), "ls-remote", repo, "HEAD"],
                    os.getcwd(), extra_env=_auth_env())
    if res["code"] == 0:
        return
    out = res["output"]
    if not git_token():
        raise RuntimeError(
            f"auth git vers {repo} impossible : aucun PAT configuré — binder le secret "
            f"`gitlab_token` au déploiement (env `gitlab_token`/`GITLAB_TOKEN`) ou passer "
            f"le PAT dans l'URL du repo (https://oauth2:<PAT>@host/...git) : {out}")
    raise RuntimeError(
        f"PAT refusé par le forge pour {repo} (expiré/révoqué, ou scopes "
        f"read_repository/write_repository/api manquants — lu depuis la variable "
        f"`{git_token_source()}`) : {out}")


@workflows.activity()
async def ensure_workspace(repo: str) -> dict:
    """Si `repo` est une URL (GitLab/GitHub), clone le repo dans un workspace
    local (`pipeline_workspace`, défaut /tmp/pipeline-workspaces) et retourne le
    chemin. Si c'est un chemin local, le retourne tel quel (mode local)."""
    global _URL_TOKEN
    if not is_url(repo):
        return {"path": repo, "url": None}
    repo, url_token = extract_url_token(repo)  # PAT embarqué dans l'URL (jamais loggué)
    if url_token:
        _URL_TOKEN = url_token
    repo = normalize_repo_url(repo)   # git@... -> https://... (auth PAT, pas SSH)
    await _probe_remote_auth(repo)
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    digest = hashlib.md5(repo.encode()).hexdigest()[:8]
    root = _env("pipeline_workspace", "PIPELINE_WORKSPACE") \
        or os.path.join(tempfile.gettempdir(), "pipeline-workspaces")
    dest = os.path.join(root, f"{name}-{digest}")
    if os.path.isdir(os.path.join(dest, ".git")):
        await _sh(["git", *_auth_args(), "fetch", "origin"], dest, extra_env=_auth_env())
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
        await _sh(["git", "reset", "--hard", "origin/" + await base_branch(dest)], dest)
    else:
        os.makedirs(root, exist_ok=True)
        res = await _sh(["git", *_auth_args(), "clone", repo, dest], root, extra_env=_auth_env())
        if res["code"] != 0:
            out = res["output"]
            hints = []
            if "Host key verification failed" in out or "publickey" in out:
                hints.append("auth SSH inattendue (les URL git@ sont normalisées en HTTPS)")
            if "could not read Username" in out or "Authentication failed" in out \
                    or "HTTP Basic: Access denied" in out:
                src = git_token_source()
                if src:
                    hints.append(f"PAT refusé par le forge (secret lu depuis la variable "
                                 f"`{src}`) — vérifier la valeur du secret `gitlab_token` "
                                 f"(expiré/révoqué ?) et ses scopes read_repository/"
                                 f"write_repository/api")
                else:
                    hints.append("aucun PAT trouvé dans l'env du worker (variables "
                                 "`gitlab_token`/`GITLAB_TOKEN`/`pipeline_git_token` non "
                                 "renseignées) — binder le secret `gitlab_token` au "
                                 "déploiement, ou passer le PAT dans l'URL du repo : "
                                 "--repo https://oauth2:<PAT>@gitlab.toollive.bearstech.com/.../....git")
            raise RuntimeError(f"clone failed: {out}{' — ' + ' | '.join(hints) if hints else ''}")
        await _sh(["git", "checkout", "-f", await base_branch(dest)], dest)
    # Le credential persiste dans .git/config du workspace (jamais dans le
    # remote URL) : Basic PAT pour le git over HTTPS.
    if git_token():
        await _sh(["git", "config", "http.extraHeader", _auth_header()], dest)
    return {"path": dest, "url": repo, "token_present": bool(git_token())}


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
    res = await _sh(["git", *_auth_args(), "push", "-u", "origin", f"HEAD:refs/heads/{branch}"],
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
