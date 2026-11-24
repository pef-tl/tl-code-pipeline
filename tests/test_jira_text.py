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


def test_demarrage_statut_change():
    t = jira_text.start_text("TOOLLIVE-3611", "Infobulle", EXEC, True, "devs-en-cours")
    assert "Statut passé à « devs-en-cours »." in t


def test_branche_titre_commit_avec_cle_jira():
    """La clé du ticket (majuscules) relie la MR au ticket dans JIRA."""
    assert jira_text.generated_branch("TOOLLIVE-3611", "oem-status-quote-request-button-tooltip") == \
        "ia-generated/TOOLLIVE-3611-oem-status-quote-request-button-tooltip"
    # change_id qui commence déjà par la clé (en minuscules) : pas de doublon
    assert jira_text.generated_branch("TOOLLIVE-3601", "toollive-3601-about-us-connected-assets-90000") == \
        "ia-generated/TOOLLIVE-3601-about-us-connected-assets-90000"
    assert jira_text.generated_branch("", "fix-x") == "ia-generated/fix-x"
    assert jira_text.mr_title("TOOLLIVE-3611", "[Test ADLC] Statuts OEM : infobulle", "x") == \
        "TOOLLIVE-3611 Statuts OEM : infobulle"
    assert jira_text.mr_title("", "", "fix-x") == "[fix-x] pipeline spec-to-tests"
    assert jira_text.commit_message("toollive-3611", "task t1: infobulle") == "TOOLLIVE-3611 task t1: infobulle"
    assert jira_text.commit_message("", "task t1") == "task t1"


# ------------------------------------------- commentaire « Suivi » vivant ---

def test_suivi_checklist_et_etapes():
    steps = jira_text.new_follow_steps()
    steps.update({"ticket": "done", "spec": "wait"})
    t = jira_text.follow_text("TOOLLIVE-3611", "Statuts OEM", EXEC, steps,
                              status_note="Statut passé à « devs-en-cours ».",
                              detail=jira_text.spec_ready_text(""))
    assert "Pipeline sur TOOLLIVE-3611" in t and EXEC[-8:] in t and EXEC not in t
    assert "✅ Ticket lu" in t and "👀 Spec (à valider)" in t and "⬜ Développement" in t
    assert "devs-en-cours" in t and "Spec prête" in t


def test_suivi_lien_mr_et_conclusion():
    steps = {"ticket": "done", "spec": "done", "dev": "done", "mr": "done", "review": "done"}
    t = jira_text.follow_text("TOOLLIVE-3611", "x", EXEC, steps,
                              mr_url="https://gitlab/mr/1602",
                              end_note="✅ Review approuvée — merge manuel sur le forge.")
    assert "✅ [MR](https://gitlab/mr/1602)" in t and t.endswith("merge manuel sur le forge.")


def test_suivi_sans_commande_ni_chemin():
    t = jira_text.follow_text("K-1", "x", EXEC, jira_text.new_follow_steps(),
                              detail="Transcript : python scripts/status.py X\nok")
    assert "python scripts" not in t and "ok" in t


def test_suivi_statut():
    assert jira_text.follow_status_note(True, "code-review") == "Statut passé à « code-review »."
    assert "non modifié" in jira_text.follow_status_note(False)
