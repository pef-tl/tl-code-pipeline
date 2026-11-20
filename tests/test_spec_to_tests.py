"""Tests du parsing des marqueurs de l'interview spec (workflow spec-to-tests).

Cas prod : exécution 0138f8a4...e612 / run 01a1f9a6-70e7-725a-8893-d05ac167ac5a
(2026-11-20T15:49Z) — l'agent spec a renvoyé du contenu (2721 chars au dernier
tour, visible dans Studio) mais sans ligne commençant exactement par
« QUESTION: » / « CHANGE_ID: » : l'ancien parsing le comptait comme « réponse
vide » et escaladait après 3 tours. Le parsing doit tolérer le markdown, l'espace
avant les deux-points et la casse.

Run : uv run --with pytest python -m pytest tests/ -q
"""

from worker.workflows.spec_to_tests import _prefixed_line


def test_prefixed_line_exact():
    assert _prefixed_line("bla bla\nQUESTION: quelle stack ?\n", "QUESTION:") == "quelle stack ?"


def test_prefixed_line_french_space_before_colon():
    # typographie française fréquente côté modèle : « QUESTION : ... »
    assert _prefixed_line("réflexion\nQUESTION : quel périmètre ?", "QUESTION:") == "quel périmètre ?"


def test_prefixed_line_case_insensitive():
    assert _prefixed_line("Question: hors scope ?", "QUESTION:") == "hors scope ?"


def test_prefixed_line_markdown_bold_and_list():
    assert _prefixed_line("**QUESTION:** en gras ?", "QUESTION:") == "en gras ?"
    assert _prefixed_line("**QUESTION :** double gras ?", "QUESTION:") == "double gras ?"
    assert _prefixed_line("- QUESTION : liste ?", "QUESTION:") == "liste ?"


def test_prefixed_line_change_id():
    assert _prefixed_line("SPEC écrite.\nCHANGE_ID: fix-crtl-columns", "CHANGE_ID:") == "fix-crtl-columns"
    assert _prefixed_line("**CHANGE_ID:** **fix-crtl-columns**", "CHANGE_ID:") == "fix-crtl-columns"


def test_prefixed_line_first_match_and_no_match():
    text = "QUESTION: première ?\nprose\nQUESTION: seconde ?"
    assert _prefixed_line(text, "QUESTION:") == "première ?"
    assert _prefixed_line("aucun marqueur ici", "QUESTION:") is None
    assert _prefixed_line("", "QUESTION:") is None
    assert _prefixed_line(None, "QUESTION:") is None


def test_prefixed_line_marker_not_at_line_start_ignored():
    # le marqueur doit être en début de ligne (instruction : « exactement une ligne »)
    assert _prefixed_line("voici QUESTION: au milieu", "QUESTION:") is None
