"""Commentaires JIRA lisibles (constat TOOLLIVE-3601 à 3608 : commandes python,
chemins /tmp et diffs brutes dans les tickets)."""

from worker.activities import jira_text

EXEC = "0138f8a419a25cb5b6a051345720296b69404fc2e7e3513b82a868ffaaadff55"


def test_aucune_commande_ni_chemin_dans_les_textes():
    textes = [
        jira_text.start_text("TOOLLIVE-3607", "Faute reopenned", EXEC, False),
        jira_text.question_text("Faut-il afficher « 90 000+ » ou « 90000+ » ?"),
        jira_text.spec_ready_text(""),
        jira_text.review_fix_text(1, "https://gitlab/mr/1595"),
        jira_text.gate_fallback_text(
            "Interview spec sans réponse depuis 24h",
            f"Aucune réponse.\nTranscript : python scripts/status.py {EXEC}\n"
            f"Spec : /tmp/pipeline-workspaces/x/SPEC.md"),
    ]
    for t in textes:
        assert "python scripts" not in t
        assert "/tmp/" not in t
        assert EXEC not in t  # identifiant complet : seulement les 8 derniers caractères


def test_demarrage_statut_non_modifie():
    t = jira_text.start_text("TOOLLIVE-3607", "Faute reopenned", EXEC, False)
    assert "Statut du ticket non modifié" in t and EXEC[-8:] in t
    assert "non modifié" not in jira_text.start_text("K-1", "x", EXEC, True)


def test_spec_prete_montre_le_contenu_de_la_spec():
    spec = ("## Goal\nCorriger « Quote reopenned by » en « Quote reopened by ».\n\n"
            "## Acceptance criteria\n- **WHEN** j'ouvre l'historique en anglais\n"
            "- **THEN** je vois « Quote reopened by »\n")
    t = jira_text.spec_ready_text(spec)
    assert "Ce qui va être fait" in t and "Quote reopened by" in t
    assert "- QUAND j'ouvre l'historique en anglais ALORS je vois « Quote reopened by »" in t


def test_question_citee():
    t = jira_text.question_text("Format « 90 000+ » ?")
    assert "> Format « 90 000+ » ?" in t and "reply" in t


def test_diff_retiree():
    payload = ("Lien\n\ndiff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
               "Tests:\nok")
    out = jira_text.strip_technical(payload)
    assert "diff --git" not in out and "@@" not in out and "+++ b/x" not in out
    assert "Lien" in out and "Tests:" in out
