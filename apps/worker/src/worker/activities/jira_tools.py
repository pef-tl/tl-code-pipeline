"""Activities JIRA via le connecteur MCP `atlassian` (Studio > Connectors).

Aucun credential dans le code : le slot `atlassian_connector` est déclaré ici et
attaché au workflow par `@uses_connectors` (voir workflows/spec_to_tests.py) ; la
plateforme résout les credentials au runtime selon l'identité du run (utilisateur
déclencheur en OBO, worker sinon). En local (`mistral dev`) le worker tourne sous
l'identité de l'utilisateur : le credential `atlassian` connecté dans Studio suffit.
En déploiement managé (run non attended), il faut un credential `atlassian` au
scope workspace/organisation pour que le service account du worker le résolve.

Le slot est déclaré `run_as="deployment"` (identité service du worker — aucun flux
OAuth interactif possible en non attended) avec `auto_auth=False` : le preflight
auto_auth du SDK liste les credentials au scope USER
(`GET /v1/connectors/<id>/user/credentials`), qui répond 404 « No consumer found
when listing credentials for type user » quand il est appelé par le service account
d'un déploiement managé, et cette erreur d'activité fait échouer l'exécution entière
avant même la première activité (exécution `0138f8a4...860559`, run
`01a1eef1-1481-701a-bf52-fead246a72ec`, 2026-11-18T13:55:39Z). Sans preflight, les
appels d'outils passent par la gateway du connecteur
(`POST /v1/connectors/<id>/tools/<tool>/call`), qui résout côté serveur le credential
épinglé par `credentials_name` (env `jira_credentials_name`, si défini) ou, à défaut,
le credential par défaut du caller (user, puis workspace, puis organisation). Nom du
connecteur : env `jira_connector_name` (défaut `01a1ef7f-0488-7652-934c-83b2eca95331`
= connecteur workspace `atlassian_94e0`, serveur `https://mcp.atlassian.com/v2/mcp`,
auth configurée au niveau du connecteur — utilisable par le service account d'un
déploiement managé).

Quand aucun credential n'est utilisable sous l'identité du run (connecteur OAuth
user-only, ex. déploiement managé sans credential workspace), l'appel d'outil renvoie
401 « Authentication failed against MCP server while calling tool » (exécution
`0138f8a4...66ea`, run `01a1ef07-2634-701c-9223-1320f1e38a36`, 2026-11-18T14:19Z) :
`fetch_jira_issue` traite alors le connecteur comme indisponible
(`{"ok": False, "reason": "connector_unavailable"}`) et le run continue sans le ticket,
au lieu d'échouer entièrement. Un ticket introuvable (mauvaise clé) reste fatal.
Même traitement quand la gateway répond 200 mais que le tool MCP renvoie une erreur
d'auth DANS son contenu (`ConnectorToolCallError`, réponse dans `details`, invisible
dans `str(exc)`) — ex. « You don't have permission to connect via API token. Please
ask your organization admin for access. » quand le connecteur `atlassian_94e0`
s'authentifie auprès de mcp.atlassian.com avec un API token refusé par l'org
(exécution `0138f8a4...767f50`, run `01a1ef65-f20c-728a-957f-da137a617e40`,
2026-11-18T16:03Z) : `_is_connector_unavailable` inspecte `str(exc)` ET le texte des
`details` de l'exception.
Même traitement quand le tool renvoie une erreur de scopes insuffisants (403
« Insufficient scopes for "getJiraIssue". Required: [read:jira:agent-interface] » —
credential résolu sur le connecteur `atlassian_49bb` sans le scope demandé ;
exécution `0138f8a4...c2740`, run `01a1ef8e-f451-75d8-b273-e66aae99cec2`,
2026-11-18T16:48Z) : marqueur « insufficient scope ».

Outils MCP utilisés (connecteur Atlassian) :
- lecture  : getJiraIssue, searchJiraIssuesUsingJql, getTransitionsForJiraIssue
- écriture : addCommentToJiraIssue, transitionJiraIssue

Sur le serveur MCP v2 (mcp.atlassian.com/v2/mcp — ex. connecteur custom
`atlassian_94e0`), deux outils sont renommés avec les mêmes arguments :
getTransitionsForJiraIssue → listJiraIssueTransitions et
addCommentToJiraIssue → addOrEditJiraIssueComment ; `_call` retombe sur ces noms
automatiquement. `transitionJiraIssue` v2 prend `transitionId` au lieu de
`transition={"id": ...}` (fallback géré dans `_transition`).

`cloudId` : env `jira_cloud_id` (UUID ou host du site, ex. `toollive.atlassian.net`),
sinon le host de `jira_base_url` (défaut https://toollive.atlassian.net/jira).

Convention : toutes les variables d'env du pipeline sont en minuscules, avec
fallback sur l'ancien nom en majuscules.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import ConnectorSlot, ToolCallClient, connector

DEFAULT_JIRA_BASE_URL = "https://toollive.atlassian.net/jira"
# Connecteur workspace `atlassian_94e0` (id 01a1ef49-...), serveur
# https://mcp.atlassian.com/v2/mcp, auth au niveau du connecteur (service account).
DEFAULT_JIRA_CONNECTOR_NAME = "01a1ef7f-0488-7652-934c-83b2eca95331"

ISSUE_FIELDS = ["summary", "description", "status", "issuetype", "priority",
                "labels", "assignee", "reporter", "comment"]
SEARCH_FIELDS = ["summary", "status", "issuetype", "priority", "labels", "assignee"]


def _env(*names: str) -> str | None:
    """Première variable d'env non vide parmi `names` (convention minuscule,
    fallback sur l'ancien nom en majuscules)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


def _build_atlassian_connector() -> ConnectorSlot:
    """Slot connecteur JIRA pour runs managés non attended : identité service du
    worker (`run_as="deployment"`), preflight auto_auth désactivé (son listing des
    credentials au scope user 404 pour le service account et tue l'exécution).
    Nom/ID du connecteur : env `jira_connector_name` (défaut `01a1ef49-...` =
    `atlassian_94e0`, serveur mcp.atlassian.com/v2/mcp). Credential : épinglé par nom
    seulement si `jira_credentials_name` est défini, sinon la gateway résout le
    credential par défaut (user, puis workspace, puis organisation)."""
    return connector(
        _env("jira_connector_name", "JIRA_CONNECTOR_NAME") or DEFAULT_JIRA_CONNECTOR_NAME,
        run_as="deployment",
        auto_auth=False,
        credentials_name=_env("jira_credentials_name", "JIRA_CREDENTIALS_NAME"),
    )


# Slot déclaré au niveau module ; attaché au workflow via @uses_connectors(...).
atlassian_connector = _build_atlassian_connector()


def cloud_id() -> str:
    """cloudId Atlassian (UUID ou host du site) : env `jira_cloud_id`, sinon le
    host de `jira_base_url` (défaut https://toollive.atlassian.net/jira)."""
    cid = _env("jira_cloud_id", "JIRA_CLOUD_ID")
    if cid:
        return cid
    base = _env("jira_base_url", "JIRA_BASE_URL") or DEFAULT_JIRA_BASE_URL
    return urllib.parse.urlparse(base).hostname or base


def issue_url(issue_key: str) -> str:
    """URL web du ticket (`https://<site>/jira/browse/<KEY>`)."""
    base = (_env("jira_base_url", "JIRA_BASE_URL") or DEFAULT_JIRA_BASE_URL).rstrip("/")
    return f"{base}/browse/{issue_key}"


_INLINE_TYPES = {"text", "mention", "emoji", "date", "status", "inlineCard", "hardBreak"}


def _adf_to_text(node: Any) -> str:
    """Convertit un noeud ADF (Atlassian Document Format) en texte brut — utile si
    l'API renvoie du JSON ADF au lieu du markdown demandé. Les noeuds inline sont
    concaténés tels quels, les noeuds bloc sont séparés par un saut de ligne."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("text") is not None:
            return str(node["text"])
        out = ""
        for child in node.get("content") or []:
            ctext = _adf_to_text(child)
            if not ctext:
                continue
            ctype = child.get("type") if isinstance(child, dict) else None
            if ctype in _INLINE_TYPES:
                out += ctext
            else:
                if out and not out.endswith("\n"):
                    out += "\n"
                out += ctext
        return out
    if isinstance(node, list):
        return "\n".join(t for t in (_adf_to_text(n) for n in node) if t)
    return str(node)


def _blocks_text(dumped: Any) -> str:
    """Texte concaténé des blocs `content` d'une réponse de tool MCP."""
    content = dumped.get("content") if isinstance(dumped, dict) else getattr(dumped, "content", None)
    parts = []
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text if isinstance(text, str) else str(text))
    return "\n".join(parts)


def _unwrap_data_envelope(parsed: Any) -> Any:
    """Développe l'enveloppe `{"data": ...}` du serveur MCP v2 d'Atlassian
    (mcp.atlassian.com/v2/mcp) : getJiraIssue y renvoie le ticket sous
    `{"data": {"appliedContentFormat": ..., "id": ..., "key": ..., "fields": ...}}`
    (cas prod exécution `0138f8a4...a2a9c` / run `01a1effa-ca7a-773e-ada1-335119a7aae7` où le
    ticket TOOLLIVE-3601 était bien présent mais déclaré « introuvable » car `key` n'était
    pas au premier niveau). On ne développe que quand `data` est la seule clé,
    pour ne jamais déballer un payload d'erreur (`errorMessages`, `error`, ...)."""
    if (isinstance(parsed, dict) and set(parsed) == {"data"}
            and isinstance(parsed["data"], (dict, list))):
        return parsed["data"]
    return parsed


def _parse_tool_result(dumped: Any) -> Any:
    """Résultat d'un call_tool : JSON parsé (enveloppe `data` développée) si le
    texte est du JSON, sinon texte brut."""
    text = _blocks_text(dumped)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return _unwrap_data_envelope(parsed)


# Le serveur MCP v2 d'Atlassian (mcp.atlassian.com/v2/mcp — ex. connecteur custom
# `atlassian_94e0`) a renommé deux outils par rapport au connecteur builtin v1,
# avec les mêmes arguments : getTransitionsForJiraIssue -> listJiraIssueTransitions,
# addCommentToJiraIssue -> addOrEditJiraIssueComment.
_TOOL_FALLBACKS = {
    "getTransitionsForJiraIssue": "listJiraIssueTransitions",
    "addCommentToJiraIssue": "addOrEditJiraIssueComment",
}


def _is_unknown_tool(exc: BaseException) -> bool:
    """Vrai si l'outil demandé n'existe pas sur le connecteur : 404 de la gateway
    (« Tool ... not found ») OU erreur MCP renvoyée DANS le contenu du tool avec un
    HTTP 200 de la gateway (serveur v2 : « Unknown tool: '<nom>' » ; cas prod exécution
    `0138f8a4...b2363` / run `01a1f00d-42f0-7593-8576-dbf2d3a55e33` — le texte est dans
    `details` de la ConnectorToolCallError, invisible dans str(exc), d'où `_exc_text`)."""
    if getattr(exc, "status_code", None) == 404 and "tool" in str(exc).lower():
        return True
    msg = _exc_text(exc).lower()
    return ("tool" in msg and "not found" in msg) or "unknown tool" in msg


async def _call(jira: ToolCallClient, tool: str, **arguments: Any) -> Any:
    """Appelle un outil du connecteur et parse sa réponse (JSON ou texte). Si le
    serveur ne connaît pas le nom (v1), essaie le nom de fallback v2."""
    try:
        return _parse_tool_result(await jira.call_tool(tool_name=tool, arguments=arguments))
    except Exception as exc:
        fallback = _TOOL_FALLBACKS.get(tool)
        if fallback is None or not _is_unknown_tool(exc):
            raise
        try:
            return _parse_tool_result(await jira.call_tool(tool_name=fallback, arguments=arguments))
        except Exception as fallback_exc:
            raise exc from fallback_exc


def _brief(issue: dict) -> dict:
    """Vue synthétique d'un ticket (résultats de recherche)."""
    fields = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "issuetype": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "labels": fields.get("labels") or [],
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "url": issue_url(issue.get("key") or ""),
    }


def _issue_detail(issue: dict) -> dict:
    """Ticket complet normalisé (getJiraIssue) : description markdown + commentaires."""
    fields = issue.get("fields") or {}
    desc = fields.get("description")
    if isinstance(desc, dict):
        desc = _adf_to_text(desc)
    comments = (fields.get("comment") or {}).get("comments") or issue.get("comments") or []
    detail = _brief(issue)
    detail.update({
        "id": issue.get("id"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "description": desc or "",
        "comments": [
            {
                "author": (c.get("author") or {}).get("displayName"),
                "body": (_adf_to_text(c.get("body")) if isinstance(c.get("body"), dict)
                         else (c.get("body") or "")),
            }
            for c in comments
        ],
    })
    return detail


async def _fetch_issue(jira: ToolCallClient, issue_key: str) -> dict:
    result = await _call(
        jira, "getJiraIssue",
        cloudId=cloud_id(), issueIdOrKey=issue_key,
        fields=ISSUE_FIELDS, responseContentFormat="markdown",
    )
    if not isinstance(result, dict) or "key" not in result:
        raise RuntimeError(
            f"ticket JIRA {issue_key!r} introuvable ou illisible: {str(result)[:400]}")
    return _issue_detail(result)


_CONNECTOR_UNAVAILABLE_MARKERS = (
    "authentication failed against mcp server",
    "no connection this caller can use",
    "no consumer found",
    "not found for connector",
    "permission to connect",
    "connect via api token",
    "api token authentication",
    "authentication failed",
    "authentication required",
    "not authenticated",
    "insufficient scope",
    "use discover to find endpoints you have access to",
)


def _exc_text(exc: BaseException) -> str:
    """Texte complet d'une exception : son message plus le contenu des détails
    qu'elle embarque. `ConnectorToolCallError` (tool MCP renvoyé en erreur malgré un
    HTTP 200 de la gateway) porte la réponse MCP — et son message d'erreur d'auth —
    dans `details`, invisible dans `str(exc)`."""
    parts = [str(exc)]
    for carrier in (getattr(exc, "response", None), getattr(exc, "details", None)):
        if carrier is None:
            continue
        items = carrier if isinstance(carrier, (list, tuple)) else (carrier,)
        for item in items:
            try:
                text = _blocks_text(item)
            except Exception:
                text = ""
            parts.append(text or str(item))
    return "\n".join(p for p in parts if p)


def _is_connector_unavailable(exc: BaseException) -> bool:
    """Vrai si l'erreur vient de l'identité du run sur le connecteur et non du ticket
    demandé : 401/403 de la gateway, credential introuvable pour ce caller (ex.
    déploiement managé sans credential workspace sur un connecteur OAuth user-only —
    exécution `0138f8a4...66ea`, run `01a1ef07-2634-701c-9223-1320f1e38a36`), ou erreur
    d'auth renvoyée DANS le contenu du tool MCP (HTTP 200, `ConnectorToolCallError`
    dont la réponse est dans `details`) — ex. « You don't have permission to connect via
    API token. Please ask your organization admin for access. » quand le connecteur
    `atlassian_94e0` s'authentifie auprès de mcp.atlassian.com avec un API token refusé
    par l'org (exécution `0138f8a4...767f50`, run
    `01a1ef65-f20c-728a-957f-da137a617e40`, 2026-11-18T16:03Z). Même traitement pour
    une erreur de scopes insuffisants renvoyée dans le contenu du tool (403
    « Insufficient scopes for "getJiraIssue". Required: [read:jira:agent-interface] » —
    credential résolu sur le connecteur `atlassian_49bb` sans le scope demandé ;
    exécution `0138f8a4...c2740`, run `01a1ef8e-f451-75d8-b273-e66aae99cec2`,
    2026-11-18T16:48Z). Un ticket introuvable (mauvaise clé) reste fatal."""
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    msg = _exc_text(exc).lower()
    return any(marker in msg for marker in _CONNECTOR_UNAVAILABLE_MARKERS)


async def _fetch_issue_or_unavailable(jira: ToolCallClient, issue_key: str) -> dict:
    """Lit le ticket ; si le connecteur est inutilisable sous l'identité du run,
    retourne {"ok": False, "reason": "connector_unavailable", ...} au lieu de faire
    échouer l'exécution (le pipeline continue sans le ticket). Un ticket
    introuvable (mauvaise clé en entrée) reste une erreur fatale."""
    try:
        return await _fetch_issue(jira, issue_key)
    except Exception as exc:
        if _is_connector_unavailable(exc):
            return {"ok": False, "reason": "connector_unavailable", "error": _exc_text(exc)[:300]}
        raise


async def _search(jira: ToolCallClient, jql: str, max_results: int) -> list:
    result = await _call(
        jira, "searchJiraIssuesUsingJql",
        cloudId=cloud_id(), jql=jql, maxResults=max_results,
        fields=SEARCH_FIELDS, responseContentFormat="markdown",
    )
    issues = result.get("issues") if isinstance(result, dict) else None
    return [_brief(i) for i in issues or []]


async def _comment(jira: ToolCallClient, issue_key: str, body: str) -> dict:
    """Poste un commentaire markdown. NON bloquant : en cas d'erreur, retourne
    {"ok": False, "error": ...} sans faire échouer le pipeline."""
    try:
        await _call(jira, "addCommentToJiraIssue",
                    cloudId=cloud_id(), issueIdOrKey=issue_key,
                    commentBody=body, contentFormat="markdown")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


async def _transition(jira: ToolCallClient, issue_key: str, transition_name: str) -> dict:
    """Transition de statut par NOM (ex. « In Progress », « code-review ») : résout
    l'id via getTransitionsForJiraIssue (v2 : listJiraIssueTransitions) puis appelle
    transitionJiraIssue (v1 : transition={"id": ...}, v2 : transitionId=...). Si la
    transition n'est pas disponible sur le ticket : {"ok": False, "reason": ...}."""
    if not transition_name:
        return {"ok": False, "reason": "nom de transition vide"}
    try:
        transitions = await _call(jira, "getTransitionsForJiraIssue",
                                  cloudId=cloud_id(), issueIdOrKey=issue_key)
        items = transitions.get("transitions") if isinstance(transitions, dict) else transitions
        wanted = transition_name.strip().lower()
        match = None
        for t in items or []:
            names = {str(t.get("name", "")).lower(),
                     str((t.get("to") or {}).get("name", "")).lower()}
            if wanted in names:
                match = t
                break
        if not match:
            return {"ok": False,
                    "reason": f"transition {transition_name!r} indisponible sur {issue_key}"}
        try:
            await _call(jira, "transitionJiraIssue",
                        cloudId=cloud_id(), issueIdOrKey=issue_key,
                        transition={"id": match["id"]})
        except Exception:
            await _call(jira, "transitionJiraIssue",
                        cloudId=cloud_id(), issueIdOrKey=issue_key,
                        transitionId=str(match["id"]))
        return {"ok": True, "transition": match.get("name"),
                "to": (match.get("to") or {}).get("name")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@workflows.activity()
async def fetch_jira_issue(issue_key: str,
                           jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Lit un ticket JIRA (getJiraIssue) et le normalise. Échoue fort si le ticket
    est introuvable — une mauvaise clé passée en entrée doit arrêter le run. Si le
    connecteur est inutilisable sous l'identité du run (401/403 gateway, ou erreur
    d'auth MCP renvoyée dans le contenu du tool), retourne
    {"ok": False, "reason": "connector_unavailable"} : le run continue sans JIRA."""
    return await _fetch_issue_or_unavailable(jira, issue_key)


@workflows.activity()
async def jira_search(jql: str, max_results: int = 10,
                      jira: ToolCallClient = Depends(atlassian_connector)) -> list:
    """Recherche JQL (searchJiraIssuesUsingJql), ex.
    `project = TOOLLIVE AND statusCategory = "To Do" ORDER BY created DESC`."""
    return await _search(jira, jql, max_results)


@workflows.activity()
async def jira_comment(issue_key: str, body: str,
                       jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Poste un commentaire markdown sur le ticket (addCommentToJiraIssue).
    Non bloquant pour le pipeline (voir _comment)."""
    return await _comment(jira, issue_key, body)


@workflows.activity()
async def jira_transition(issue_key: str, transition_name: str,
                          jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition de statut par nom explicite (voir _transition)."""
    return await _transition(jira, issue_key, transition_name)


@workflows.activity()
async def jira_transition_start(issue_key: str,
                                jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition de démarrage du run : env `jira_transition_start` (défaut
    « In Progress »)."""
    name = _env("jira_transition_start", "JIRA_TRANSITION_START") or "In Progress"
    return await _transition(jira, issue_key, name)


@workflows.activity()
async def jira_transition_review(issue_key: str,
                                 jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition à l'ouverture de la MR : env `jira_transition_review` (défaut
    « code-review »)."""
    name = _env("jira_transition_review", "JIRA_TRANSITION_REVIEW") or "code-review"
    return await _transition(jira, issue_key, name)


@workflows.activity()
async def jira_transition_done(issue_key: str,
                               jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition finale après approbation : env `jira_transition_done` (défaut
    vide = aucune transition, le merge reste manuel)."""
    name = _env("jira_transition_done", "JIRA_TRANSITION_DONE") or ""
    return await _transition(jira, issue_key, name)


# ---------------------------------------------------------------- Confluence ---
# Base de connaissances (ticket « Intégration de JIRA » : utiliser Confluence en
# amont pour planifier, en aval pour compléter la doc). LECTURE SEULE ici : le
# pipeline n'écrit jamais dans Confluence ; il cite les pages liées au ticket
# (contexte pour l'agent spec, et « doc à vérifier » dans le rapport QA).

def confluence_query(summary: str) -> str:
    """Texte de recherche à partir du titre du ticket : sans préfixe [..], sans
    guillemets (injection CQL), 100 caractères max."""
    text = re.sub(r"\[[^\]]*\]", " ", summary or "")
    text = re.sub(r"[\"'«»“”\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:100]


def _confluence_pages(result: Any, limit: int) -> list[dict]:
    """Pages {title, url, excerpt} trouvées dans une réponse de recherche, quel
    que soit son format (v1 `results[].content`, v2 `results[]`, liste brute)."""
    pages: list[dict] = []
    base = ""
    if isinstance(result, dict):
        base = ((result.get("_links") or {}).get("base") or "").rstrip("/")
        items = result.get("results") or result.get("pages") or result.get("items") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title") or item.get("title")
        if not title:
            continue
        links = content.get("_links") or item.get("_links") or {}
        url = (item.get("url") or content.get("url") or item.get("webUrl")
               or links.get("webui") or "")
        if url.startswith("/"):
            url = f"{base}{url}" if base else url
        excerpt = re.sub(r"@@@(end)?hl@@@", "", str(item.get("excerpt") or ""))
        pages.append({"title": str(title).strip(), "url": url,
                      "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:200]})
        if len(pages) >= limit:
            break
    return pages


async def _confluence_search(jira: ToolCallClient, summary: str, limit: int) -> dict:
    query = confluence_query(summary)
    if not query:
        return {"ok": False, "error": "titre vide", "pages": []}
    # Serveur MCP v2 (mcp.atlassian.com/v2) : `searchConfluence` exige `cql`
    # (constat exécution 65fe3c32 : « Invalid arguments for tool searchConfluence:
    # Required at cql ») ; v1 : `searchConfluenceUsingCql`, mêmes arguments.
    cql = f'type = page AND text ~ "{query}"'
    attempts = [
        ("searchConfluence", {"cloudId": cloud_id(), "cql": cql, "limit": limit}),
        ("searchConfluenceUsingCql", {"cloudId": cloud_id(), "cql": cql, "limit": limit}),
    ]
    last: BaseException | None = None
    for tool, args in attempts:
        try:
            return {"ok": True, "pages": _confluence_pages(await _call(jira, tool, **args), limit)}
        except Exception as exc:  # outil absent, scopes, réseau : on tente le suivant
            last = exc
    return {"ok": False, "error": _exc_text(last)[:300] if last else "", "pages": []}


@workflows.activity()
async def confluence_search(summary: str, limit: int = 3,
                            jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Pages Confluence liées au titre du ticket. NON bloquant : en cas d'erreur,
    {"ok": False, "pages": []} et le pipeline continue."""
    return await _confluence_search(jira, summary, limit)
