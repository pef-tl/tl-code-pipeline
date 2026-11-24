"""Scrutateur JIRA (jira-watch) : choix du repo, prise du ticket avant le
lancement, quotas, échecs — sans Temporal ni réseau.

Run : uv run --with pytest python -m pytest tests/ -q
"""

import asyncio
import json

import pytest

import worker.activities.jira_watch as jwa
import worker.workflows.jira_watch as watch_mod
from worker.workflows.jira_watch import JiraWatch


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("jira_watch_jql", "jira_watch_default_repo",
                 "gitlab_c8y_repo", "gitlab_micro_repo"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("gitlab_c8y_repo", "https://gitlab.example/toollive-c8y.git")
    monkeypatch.setenv("gitlab_micro_repo", "https://gitlab.example/toollive-c8y-microservice.git")


# ----------------------------------------------------------- partie pure ---

def test_jql_par_defaut_a_faire_et_etiquette_agent():
    jql = jwa.watch_jql()
    assert 'status = "A faire"' in jql and "labels = agent" in jql


def test_jql_configurable(monkeypatch):
    monkeypatch.setenv("jira_watch_jql", "project = TOOLLIVE AND labels = robot")
    assert jwa.watch_jql() == "project = TOOLLIVE AND labels = robot"


@pytest.mark.parametrize("labels,alias", [
    (["agent", "micro"], "micro"),
    (["agent", "Microservice"], "micro"),
    (["agent", "c8y"], "c8y"),
    (["agent"], "c8y"),
    (None, "c8y"),
])
def test_repo_d_apres_les_etiquettes(labels, alias):
    assert jwa.repo_alias(labels) == alias


def test_repo_par_defaut_configurable(monkeypatch):
    monkeypatch.setenv("jira_watch_default_repo", "micro")
    assert jwa.repo_alias(["agent"]) == "micro"


def test_resolution_du_repo(monkeypatch):
    assert jwa.resolve_repo("c8y").endswith("toollive-c8y.git")
    assert jwa.resolve_repo("https://gitlab.example/x.git") == "https://gitlab.example/x.git"
    monkeypatch.delenv("gitlab_micro_repo")
    assert jwa.resolve_repo("micro") == ""


def test_candidats():
    out = jwa.candidates([{"key": "TOOLLIVE-1", "summary": "S", "labels": ["agent", "micro"]},
                          {"summary": "sans clé"}])
    assert out == [{"key": "TOOLLIVE-1", "summary": "S", "repo_alias": "micro",
                    "repo": "https://gitlab.example/toollive-c8y-microservice.git"}]
    assert "TOOLLIVE-1" in jwa.request_for(out[0])


def test_activite_recherche_non_bloquante():
    class Broken:
        async def call_tool(self, tool_name, arguments=None):
            raise RuntimeError("Authentication failed against MCP server")

    out = asyncio.run(jwa._watch_candidates(Broken(), "", 10))
    assert out["ok"] is False and "Authentication failed" in out["error"]


# ------------------------------------------------------------- workflow ---

def _run_watch(monkeypatch, tickets, move_results=None, start_error=None, **kwargs):
    calls = {"moves": [], "starts": [], "comments": []}
    move_results = move_results or {}

    async def fake_candidates(jql, max_results):
        return {"ok": True, "jql": jql or "JQL", "tickets": tickets}

    async def fake_move(key, phase, by_human=False):
        calls["moves"].append((key, phase, by_human))
        return move_results.get((key, phase), {"ok": True, "from": "A faire", "to": "Pris en charge"})

    async def fake_comment(key, body):
        calls["comments"].append((key, body))
        return {"ok": True}

    class Handle:
        def __init__(self, key):
            self.id = f"exec-{key}"

    async def fake_execute(workflow, params, execution_timeout=None, execution_id=None,
                           wait=True, parent_close_policy=None):
        assert wait is False
        if start_error:
            raise start_error
        calls["starts"].append(json.loads(params.model_dump_json()))
        return Handle(params.jira_issue)

    monkeypatch.setattr(watch_mod.jira_watch, "jira_watch_candidates", fake_candidates)
    monkeypatch.setattr(watch_mod.jira_tools, "jira_move", fake_move)
    monkeypatch.setattr(watch_mod.jira_tools, "jira_comment", fake_comment)
    monkeypatch.setattr(watch_mod.workflows.workflow, "execute_workflow", fake_execute)
    run = JiraWatch.run
    while hasattr(run, "__wrapped__"):
        run = run.__wrapped__
    report = asyncio.run(run(JiraWatch(), **kwargs))
    return report, calls


T1 = {"key": "TOOLLIVE-1", "summary": "Un", "repo_alias": "c8y", "repo": "https://g/c8y.git"}
T2 = {"key": "TOOLLIVE-2", "summary": "Deux", "repo_alias": "micro", "repo": "https://g/micro.git"}
T3 = {"key": "TOOLLIVE-3", "summary": "Trois", "repo_alias": "c8y", "repo": "https://g/c8y.git"}


def test_prend_le_ticket_avant_de_lancer_le_pipeline(monkeypatch):
    report, calls = _run_watch(monkeypatch, [T1])
    assert calls["moves"] == [("TOOLLIVE-1", "start", False)]  # pris par l'agent, pas un humain
    assert calls["starts"] == [{"repo": "https://g/c8y.git",
                                "request": jwa.request_for(T1), "jira_issue": "TOOLLIVE-1"}]
    assert report["started"] == [{"key": "TOOLLIVE-1", "repo": "c8y", "execution_id": "exec-TOOLLIVE-1"}]


def test_quota_par_passage(monkeypatch):
    report, calls = _run_watch(monkeypatch, [T1, T2, T3], max_starts=2)
    assert [s["jira_issue"] for s in calls["starts"]] == ["TOOLLIVE-1", "TOOLLIVE-2"]
    assert report["skipped"] == [{"key": "TOOLLIVE-3", "reason": "quota du passage atteint"}]


def test_ticket_refuse_par_la_garde_n_est_pas_lance(monkeypatch):
    refused = {("TOOLLIVE-1", "start"): {"ok": False, "refused": True,
                                         "reason": "garde workflow v4 : réservée à un humain"}}
    report, calls = _run_watch(monkeypatch, [T1], move_results=refused)
    assert calls["starts"] == []
    assert "réservée à un humain" in report["skipped"][0]["reason"]


def test_ticket_deja_pris_n_est_pas_relance(monkeypatch):
    already = {("TOOLLIVE-1", "start"): {"ok": True, "skipped": True, "to": "Pris en charge"}}
    report, calls = _run_watch(monkeypatch, [T1], move_results=already)
    assert calls["starts"] == [] and report["skipped"][0]["reason"] == "déjà pris"


def test_repo_non_resolu_signale(monkeypatch):
    report, calls = _run_watch(monkeypatch, [dict(T2, repo="")])
    assert calls["moves"] == [] and calls["starts"] == []
    assert "gitlab_micro_repo" in report["skipped"][0]["reason"]


def test_dry_run_ne_touche_a_rien(monkeypatch):
    report, calls = _run_watch(monkeypatch, [T1, T2], dry_run=True)
    assert calls["moves"] == [] and calls["starts"] == []
    assert [s["key"] for s in report["started"]] == ["TOOLLIVE-1", "TOOLLIVE-2"]


def test_echec_du_demarrage_bloque_le_ticket(monkeypatch):
    report, calls = _run_watch(monkeypatch, [T1], start_error=RuntimeError("quota worker"))
    assert calls["moves"] == [("TOOLLIVE-1", "start", False), ("TOOLLIVE-1", "blocked", False)]
    assert "quota worker" in calls["comments"][0][1]
    assert report["started"] == [] and "quota worker" in report["skipped"][0]["reason"]


def test_connecteur_indisponible(monkeypatch):
    async def broken(jql, max_results):
        return {"ok": False, "jql": "JQL", "error": "401", "tickets": []}

    monkeypatch.setattr(watch_mod.jira_watch, "jira_watch_candidates", broken)
    run = JiraWatch.run
    while hasattr(run, "__wrapped__"):
        run = run.__wrapped__
    report = asyncio.run(run(JiraWatch()))
    assert report["error"] == "401" and report["started"] == []
