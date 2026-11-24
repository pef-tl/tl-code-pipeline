"""Workflow JIRA v4 : garde côté code (transitions autorisées, gardes humaines),
changement de statut gardé (_move) et passage en « blocked » côté workflow.

Run : uv run --with pytest python -m pytest tests/ -q
"""

import asyncio
import json

import pytest

import worker.activities.jira_tools as jt
import worker.activities.jira_workflow as jw
import worker.workflows.spec_to_tests as wf_mod
from worker.workflows.spec_to_tests import SpecToTests


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for phase, (names, _) in jw.PHASES.items():
        for name in names:
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("jira_transition_guard", raising=False)
    monkeypatch.delenv("JIRA_TRANSITION_GUARD", raising=False)


# ------------------------------------------------------------ garde pure ---

@pytest.mark.parametrize("current,target", [
    ("A faire", "Pris en charge"),
    ("Pris en charge", "Specs"),
    ("ready-for-dev", "devs-en-cours"),
    ("devs-en-cours", "Tests auto"),
    ("Tests auto", "code-review"),
    ("code-review-in-progress", "code-review"),
    ("tested-ko", "devs-en-cours"),
])
def test_transitions_agent_autorisees(current, target):
    ok, why = jw.check_transition(current, target)
    assert ok, why


@pytest.mark.parametrize("current,target", [
    ("Specs", "ready-for-dev"),            # gate n°1
    ("code-review", "to-be-tested"),       # gate n°2
    ("code-review", "code-review-in-progress"),
    ("ready-for-val", "ready-for-prod"),
    ("Nouveau", "A faire"),
])
def test_transitions_humaines_refusees_a_l_agent(current, target):
    ok, why = jw.check_transition(current, target)
    assert not ok and "humain" in why
    assert jw.check_transition(current, target, by_human=True)[0]


def test_saut_d_etape_refuse_meme_pour_un_humain():
    ok, why = jw.check_transition("Nouveau", "ready-for-prod", by_human=True)
    assert not ok and "absente du workflow v4" in why


def test_noms_normalises():
    assert jw.check_transition("DEVS EN COURS", "TESTS-AUTO")[0]
    assert jw.check_transition("Devs_en_cours", "tests auto")[0]


def test_blocked_depuis_tout_statut_en_cours_et_sortie_humaine():
    for status in jw.IN_PROGRESS:
        assert jw.check_transition(status, "blocked")[0], status
    assert not jw.check_transition("to-be-tested", "blocked")[0]
    assert not jw.check_transition("blocked", "devs-en-cours")[0]
    assert jw.check_transition("blocked", "devs-en-cours", by_human=True)[0]


def test_statut_hors_workflow_refuse():
    assert not jw.check_transition("devs-en-cours", "Conception")[0]
    assert not jw.check_transition("Conception", "devs-en-cours")[0]


def test_statut_actuel_inconnu():
    assert jw.check_transition("", "Tests auto")[0]
    assert not jw.check_transition("", "to-be-tested")[0]


def test_chemin_apres_gate_1():
    assert jw.path_to("Specs", "devs-en-cours", by_human=True) == ["ready-for-dev", "devs-en-cours"]
    assert jw.path_to("Specs", "devs-en-cours") == []  # gate humaine requise
    ok, why = jw.check_transition("Nouveau", "Pris en charge", by_human=True)
    assert ok and "A faire" in why


def test_statut_par_phase_et_anciennes_variables(monkeypatch):
    assert jw.status_for("start") == "Pris en charge"
    assert jw.status_for("done") == ""
    monkeypatch.setenv("jira_transition_review", "Code Review")
    assert jw.status_for("review") == "Code Review"
    monkeypatch.setenv("jira_status_review", "code-review")
    assert jw.status_for("review") == "code-review"


def test_garde_desactivable(monkeypatch):
    assert jw.guard_enabled()
    monkeypatch.setenv("jira_transition_guard", "off")
    assert not jw.guard_enabled()


# --------------------------------------------- _move avec un client stub ---

def _issue(status):
    return {"key": "TOOLLIVE-3620", "fields": {"summary": "s", "status": {"name": status}}}


def _transitions(*names):
    return {"transitions": [{"id": str(i), "name": n, "to": {"name": n}}
                            for i, n in enumerate(names, 1)]}


class Stub:
    def __init__(self, status, transitions):
        self.status, self.transitions = status, transitions
        self.applied: list[str] = []

    async def call_tool(self, tool_name, arguments=None):
        args = arguments or {}
        if tool_name == "getJiraIssue" and "expand" not in args:
            payload = _issue(self.status)
        elif tool_name in ("listJiraIssueTransitions", "getTransitionsForJiraIssue"):
            payload = self.transitions
        elif tool_name == "transitionJiraIssue":
            tid = args.get("transitionId")
            name = next(t["to"]["name"] for t in self.transitions["transitions"] if t["id"] == tid)
            self.applied.append(name)
            self.status = name
            payload = {}
        else:
            raise AssertionError(f"outil inattendu {tool_name}")
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}


ALL = _transitions("A faire", "Pris en charge", "Specs", "ready-for-dev", "devs-en-cours",
                   "Tests auto", "code-review", "code-review-in-progress", "to-be-tested",
                   "blocked")


def test_move_refuse_une_gate_humaine_sans_toucher_au_ticket():
    stub = Stub("Specs", ALL)
    out = asyncio.run(jt._move(stub, "TOOLLIVE-3620", "spec_approved", by_human=False))
    assert out["ok"] is False and out["refused"] and "humain" in out["reason"]
    assert stub.applied == []


def test_move_applique_le_chemin_complet():
    stub = Stub("Nouveau", ALL)
    out = asyncio.run(jt._move(stub, "TOOLLIVE-3620", "start", by_human=True))
    assert out["ok"] is True
    assert stub.applied == ["A faire", "Pris en charge"]


def test_move_deja_dans_le_statut():
    stub = Stub("devs-en-cours", ALL)
    out = asyncio.run(jt._move(stub, "TOOLLIVE-3620", "dev"))
    assert out["ok"] and out["skipped"] and stub.applied == []


def test_move_phase_sans_statut():
    stub = Stub("code-review", ALL)
    out = asyncio.run(jt._move(stub, "TOOLLIVE-3620", "done", by_human=True))
    assert out["skipped"] and not out["ok"] and stub.applied == []


def test_move_garde_desactivee(monkeypatch):
    monkeypatch.setenv("jira_transition_guard", "off")
    stub = Stub("Specs", ALL)
    out = asyncio.run(jt._move(stub, "TOOLLIVE-3620", "spec_approved"))
    assert out["ok"] and stub.applied == ["ready-for-dev"]


# ------------------------------------------------ workflow : « blocked » ---

def test_erreur_du_pipeline_passe_le_ticket_en_blocked(monkeypatch):
    moves, notes = [], []

    async def fake_move(key, phase, by_human=False):
        moves.append((key, phase, by_human))
        return {"ok": True, "from": "devs-en-cours", "to": "blocked"}

    async def fake_follow(key, body, comment_id="", marker=""):
        notes.append(body)
        return {"ok": True, "id": "1"}

    async def boom(self, repo, request, jira_issue):
        raise RuntimeError("task t1: boucle non convergée après 3 itérations")

    monkeypatch.setattr(wf_mod.jira_tools, "jira_move", fake_move)
    monkeypatch.setattr(wf_mod.jira_tools, "jira_follow", fake_follow)
    monkeypatch.setattr(SpecToTests, "_run_pipeline", boom)
    w = SpecToTests()
    w.execution_id = "0138f8a4deadbeef"
    run = SpecToTests.run
    while hasattr(run, "__wrapped__"):
        run = run.__wrapped__
    with pytest.raises(RuntimeError, match="non convergée"):
        asyncio.run(run(w, "repo", "demande", "TOOLLIVE-3620"))
    assert moves == [("TOOLLIVE-3620", "blocked", False)]
    assert "non convergée" in notes[-1] and "Bloqué" in notes[-1]


def test_move_refuse_trace_dans_le_transcript(monkeypatch):
    async def fake_move(key, phase, by_human=False):
        return {"ok": False, "refused": True, "reason": "garde workflow v4 : réservée à un humain"}

    monkeypatch.setattr(wf_mod.jira_tools, "jira_move", fake_move)
    w = SpecToTests()
    asyncio.run(w._move("TOOLLIVE-3620", "spec_approved"))
    assert "réservée à un humain" in w.conversation[-1]["text"]
    assert "non modifié" in w.follow_status


def test_nom_interne_new_reconnu_comme_nouveau():
    """Constat TOOLLIVE-3621 : l'API renvoie « new » pour « Nouveau »."""
    from worker.activities import jira_workflow as jw
    assert jw.norm("new") == jw.norm("Nouveau")
    ok, why = jw.check_transition("new", "Pris en charge", by_human=True)
    assert ok, why
    assert jw.path_to("new", "Pris en charge", by_human=True) == ["A faire", "Pris en charge"]
    ok, _ = jw.check_transition("new", "Pris en charge", by_human=False)
    assert not ok  # Nouveau -> A faire reste une décision humaine
