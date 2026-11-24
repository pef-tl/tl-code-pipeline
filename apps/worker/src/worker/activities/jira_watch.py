"""Scrutateur JIRA : trouve les tickets confiés aux agents et choisit le repo cible.

Un ticket est confié aux agents quand il est au statut « A faire » ET porte
l'étiquette `agent` (JQL par défaut, env `jira_watch_jql`). Un ticket au statut
« Nouveau » n'est jamais pris : la qualification « Nouveau » -> « A faire » est
une décision humaine (workflow v4, transition n°1).

Repo cible déduit des étiquettes du ticket : `micro` / `microservice` -> alias
`micro`, `c8y` -> alias `c8y`, sinon env `jira_watch_default_repo` (défaut
`c8y`). L'alias est résolu côté worker via `gitlab_<alias>_repo` (URL GitLab),
comme scripts/run.py le fait côté client.

Partie pure (sans SDK ni réseau) + une activité qui appelle la recherche JQL.
"""

from __future__ import annotations

import os

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import ToolCallClient

from worker.activities import jira_tools
from worker.activities.jira_tools import atlassian_connector

DEFAULT_JQL = ('project = TOOLLIVE AND status = "A faire" AND labels = agent '
               'ORDER BY priority DESC, created ASC')

# Étiquette -> alias de repo (scripts/run.py REPOS)
LABEL_REPOS = {"micro": "micro", "microservice": "micro", "c8y": "c8y"}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        value = os.environ.get(name.upper())
    return (value or "").strip() or default


def watch_jql() -> str:
    return _env("jira_watch_jql", DEFAULT_JQL)


def repo_alias(labels: list[str] | None) -> str:
    """Alias du repo cible d'après les étiquettes (insensible à la casse)."""
    for label in labels or []:
        alias = LABEL_REPOS.get(str(label).strip().lower())
        if alias:
            return alias
    return _env("jira_watch_default_repo", "c8y")


def resolve_repo(alias: str) -> str:
    """Alias -> URL GitLab (`gitlab_<alias>_repo`) ; une URL est rendue telle
    quelle ; alias inconnu ou secret absent -> "" (ticket ignoré, signalé)."""
    if "://" in alias or alias.startswith("git@"):
        return alias
    return _env(f"gitlab_{alias}_repo")


def candidates(issues: list[dict]) -> list[dict]:
    """Tickets de la recherche -> {key, summary, repo_alias, repo} ; les tickets
    sans repo résolu sont gardés avec repo="" pour être signalés."""
    out = []
    for issue in issues:
        key = issue.get("key")
        if not key:
            continue
        alias = repo_alias(issue.get("labels"))
        out.append({"key": key, "summary": issue.get("summary") or "",
                    "repo_alias": alias, "repo": resolve_repo(alias)})
    return out


def request_for(candidate: dict) -> str:
    """Demande transmise à l'agent spec (le contenu du ticket est lu par le
    pipeline lui-même via fetch_jira_issue)."""
    return (f"Traite le ticket {candidate['key']} — {candidate['summary']}. "
            f"Lancé automatiquement par le scrutateur JIRA (étiquette « agent »).")


@workflows.activity()
async def jira_watch_candidates(jql: str = "", max_results: int = 10,
                                jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Recherche JQL des tickets confiés aux agents + repo cible de chacun.
    Non bloquant : {"ok": False, "error"} si le connecteur est indisponible."""
    return await _watch_candidates(jira, jql, max_results)


async def _watch_candidates(jira: ToolCallClient, jql: str, max_results: int) -> dict:
    query = jql or watch_jql()
    try:
        issues = await jira_tools._search(jira, query, max_results)
    except Exception as exc:
        return {"ok": False, "jql": query, "error": jira_tools._exc_text(exc)[:600],
                "tickets": []}
    return {"ok": True, "jql": query, "tickets": candidates(issues)}
