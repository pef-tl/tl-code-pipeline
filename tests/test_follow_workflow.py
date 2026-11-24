"""Commentaire « Suivi » vivant côté workflow : 1 création puis des modifications
(constat TOOLLIVE-3607 : une vingtaine de commentaires sur le ticket)."""

import asyncio

import worker.workflows.spec_to_tests as wf_mod
from worker.workflows.spec_to_tests import SpecToTests

EXEC = "0138f8a419a25cb5b6a051345720296b69404fc2e7e3513b82a868ffaaadff55"


def _run(monkeypatch, returned_id):
    calls = []

    async def fake_follow(key, body, comment_id="", marker=""):
        calls.append({"key": key, "body": body, "comment_id": comment_id})
        return {"ok": True, "id": returned_id, "edited": bool(comment_id)}

    monkeypatch.setattr(wf_mod.jira_tools, "jira_follow", fake_follow)
    w = SpecToTests()
    w.execution_id, w.jira_ref, w.jira_summary = EXEC, "TOOLLIVE-3611", "Statuts OEM"

    async def scenario():
        await w._follow("TOOLLIVE-3611", steps={"ticket": "done", "spec": "current"})
        await w._follow("TOOLLIVE-3611", detail="❓ question ?")
        await w._follow("TOOLLIVE-3611", detail="")
        await w._follow("TOOLLIVE-3611", steps={"spec": "done", "dev": "current"})
        await w._jira_note("TOOLLIVE-3611", "❌ Review rejetée.")
    asyncio.run(scenario())
    return w, calls


def test_un_seul_commentaire_modifie(monkeypatch):
    w, calls = _run(monkeypatch, "900")
    assert calls[0]["comment_id"] == ""                       # création
    assert all(c["comment_id"] == "900" for c in calls[1:])   # puis modifications
    last = calls[-1]["body"]
    assert "✅ Spec" in last and "❌ Développement" in last and "Review rejetée" in last


def test_sans_identifiant_pas_de_commentaire_par_etape(monkeypatch):
    w, calls = _run(monkeypatch, "")
    # création + question + conclusion ; pas les simples changements d'étape
    assert len(calls) == 3
    assert "question" in calls[1]["body"] and "Review rejetée" in calls[2]["body"]


def test_sans_ticket_rien(monkeypatch):
    calls = []

    async def fake_follow(*a, **k):
        calls.append(a)
        return {"ok": True, "id": "1"}
    monkeypatch.setattr(wf_mod.jira_tools, "jira_follow", fake_follow)
    asyncio.run(SpecToTests()._follow("", steps={"ticket": "done"}))
    assert calls == []
