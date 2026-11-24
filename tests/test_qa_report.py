"""Tests pytest pour le module qa_report."""

import pytest

from worker.activities.qa_report import (
    format_qa_report,
    _extract_summary,
    _extract_acceptance_criteria,
    _extract_manual_test_steps,
    _extract_modified_files,
    _format_test_results,
)


# --- Fixtures ---

@pytest.fixture
def simple_spec_content():
    """Un SPEC.md simple avec résumé et critères QUAND/ALORS."""
    return """# Modification de la page /about-us

## Résumé
Changer l'affichage du nombre de membres de "50 000+" à "90 000+" sur la page /about-us.

## Critères d'acceptation
QUAND l'utilisateur visite la page /about-us
ALORS il voit "90 000+ membres" au lieu de "50 000+"

QUAND la page est chargée
ALORS le texte est mis à jour sans erreur

## Test manuel
1. Aller sur /about-us
2. Vérifier que le texte affiche "90 000+"
"""


@pytest.fixture
def spec_with_when_then():
    """SPEC.md avec format WHEN/THEN."""
    return """# Feature: Update counter

WHEN user visits /about-us
THEN they see "90,000+ members"

WHEN page loads
THEN counter displays correctly
"""


@pytest.fixture
def fake_diff():
    """Un diff git factice."""
    return """diff --git a/pages/about-us.html b/pages/about-us.html
index 1234567..abcdefg 100644
--- a/pages/about-us.html
+++ b/pages/about-us.html
@@ -10,7 +10,7 @@
 <h1>About Us</h1>
 <p>
-We have 50 000+ members
+We have 90 000+ members
 </p>
"""


@pytest.fixture
def passed_tests_result():
    """Résultats de tests avec succès."""
    return {
        "passed": True,
        "log": "test_about_us.py::test_counter_update PASSED\ntest_about_us.py::test_page_load PASSED"
    }


@pytest.fixture
def failed_tests_result():
    """Résultats de tests avec échecs."""
    return {
        "passed": False,
        "log": "test_about_us.py::test_counter_update PASSED\ntest_about_us.py::test_page_load FAILED"
    }


@pytest.fixture
def unrunnable_tests_result():
    """Résultats de tests non exécutables."""
    return {
        "unrunnable": True,
        "log": ""
    }


@pytest.fixture
def no_tests_result():
    """Aucun test exécuté."""
    return {}


# --- Tests pour _extract_summary ---

class TestExtractSummary:
    def test_extract_summary_with_section(self, simple_spec_content):
        summary = _extract_summary(simple_spec_content)
        assert "50 000+" in summary
        assert "90 000+" in summary
        assert "page /about-us" in summary

    def test_extract_summary_without_section(self):
        content = "Première ligne de résumé\nDeuxième ligne"
        summary = _extract_summary(content)
        assert "Première ligne" in summary

    def test_extract_summary_empty(self):
        assert _extract_summary("") == "Aucun résumé disponible"

    def test_extract_summary_long(self):
        content = "A" * 300 + "\nB" * 300
        summary = _extract_summary(content)
        assert len(summary) <= 203  # 200 + "..."


# --- Tests pour _extract_acceptance_criteria ---

class TestExtractAcceptanceCriteria:
    def test_extract_criteria_quand_alors(self, simple_spec_content):
        criteria = _extract_acceptance_criteria(simple_spec_content)
        assert len(criteria) >= 2
        assert any("QUAND" in c["scenario"] and "ALORS" in c["scenario"] for c in criteria)
        assert all(c["status"] == "non testé" for c in criteria)

    def test_extract_criteria_when_then(self, spec_with_when_then):
        criteria = _extract_acceptance_criteria(spec_with_when_then)
        assert len(criteria) >= 2
        # La fonction convertit WHEN/THEN en QUAND/ALORS
        assert any("QUAND" in c["scenario"] and "ALORS" in c["scenario"] for c in criteria)

    def test_extract_criteria_empty(self):
        assert _extract_acceptance_criteria("") == []


# --- Tests pour _extract_manual_test_steps ---

class TestExtractManualTestSteps:
    def test_extract_steps_with_section(self, simple_spec_content):
        steps = _extract_manual_test_steps(simple_spec_content)
        assert len(steps) >= 2
        assert any("about-us" in step.lower() for step in steps)

    def test_extract_steps_without_section(self):
        content = "Comment tester:\n\n1. Étape un\n2. Étape deux"
        steps = _extract_manual_test_steps(content)
        assert "Étape un" in steps[0]
        assert "Étape deux" in steps[1]

    def test_extract_steps_empty(self):
        steps = _extract_manual_test_steps("")
        assert len(steps) == 1  # Retourne une instruction générique
        assert "Vérifier le fonctionnement général" in steps[0]


# --- Tests pour _extract_modified_files ---

class TestExtractModifiedFiles:
    def test_extract_files_from_diff(self, fake_diff):
        files = _extract_modified_files(fake_diff)
        assert "pages/about-us.html" in files

    def test_extract_files_empty(self):
        assert _extract_modified_files("") == []

    def test_extract_files_multiple(self):
        diff = """diff --git a/file1.txt b/file1.txt
index 1..2
--- a/file1.txt
+++ b/file1.txt
@@ -1 +1 @@
-a
+b
diff --git a/file2.txt b/file2.txt
index 1..2
--- a/file2.txt
+++ b/file2.txt
@@ -1 +1 @@
-a
+b"""
        files = _extract_modified_files(diff)
        assert "file1.txt" in files
        assert "file2.txt" in files


# --- Tests pour _format_test_results ---

class TestFormatTestResults:
    def test_format_passed_tests(self, passed_tests_result):
        results = _format_test_results(passed_tests_result)
        assert len(results) == 1
        assert "✅" in results[0]["status"]

    def test_format_failed_tests(self, failed_tests_result):
        results = _format_test_results(failed_tests_result)
        assert len(results) == 1
        assert "❌" in results[0]["status"]
        assert "✅" in results[0]["status"]

    def test_format_unrunnable_tests(self, unrunnable_tests_result):
        results = _format_test_results(unrunnable_tests_result)
        assert any("Aucun test automatique exécuté" in r["status"] for r in results)

    def test_format_no_tests(self, no_tests_result):
        results = _format_test_results(no_tests_result)
        assert any("Aucun test automatique exécuté" in r["status"] for r in results)

    def test_format_empty_tests(self):
        results = _format_test_results({})
        assert any("Aucun test automatique exécuté" in r["status"] for r in results)


# --- Tests pour format_qa_report (intégration) ---

class TestFormatQaReport:
    def test_full_report_with_all_data(
        self, simple_spec_content, fake_diff, passed_tests_result
    ):
        report = format_qa_report(
            spec_content=simple_spec_content,
            review_link="https://gitlab.com/pef-tl/tl-code-pipeline/-/merge_requests/123",
            diff_output=fake_diff,
            test_results=passed_tests_result,
        )

        # Vérifier la structure
        assert "## 📋 Rapport pour la QA" in report
        assert "**Résumé** :" in report
        assert "**MR** :" in report
        assert "https://gitlab.com/pef-tl/tl-code-pipeline/-/merge_requests/123" in report
        assert "**Fichiers modifiés** :" in report
        assert "pages/about-us.html" in report
        assert "**Critères d'acceptation** :" in report
        assert "**Tests automatiques** :" in report
        assert "✅" in report
        assert "**Comment tester à la main** :" in report

    def test_report_with_no_spec(self, fake_diff, passed_tests_result):
        report = format_qa_report(
            spec_content="",
            review_link="https://example.com/mr/1",
            diff_output=fake_diff,
            test_results=passed_tests_result,
        )
        assert "Aucun résumé disponible" in report
        assert "Aucun critère d'acceptation trouvé" in report

    def test_report_with_unrunnable_tests(
        self, simple_spec_content, fake_diff, unrunnable_tests_result
    ):
        report = format_qa_report(
            spec_content=simple_spec_content,
            review_link="https://example.com/mr/1",
            diff_output=fake_diff,
            test_results=unrunnable_tests_result,
        )
        assert "⚠️ Aucun test automatique exécuté — test manuel obligatoire" in report

    def test_report_with_failed_tests(
        self, simple_spec_content, fake_diff, failed_tests_result
    ):
        report = format_qa_report(
            spec_content=simple_spec_content,
            review_link="https://example.com/mr/1",
            diff_output=fake_diff,
            test_results=failed_tests_result,
        )
        assert "❌" in report
        assert "✅" in report

    def test_report_with_no_diff(
        self, simple_spec_content, passed_tests_result
    ):
        report = format_qa_report(
            spec_content=simple_spec_content,
            review_link="https://example.com/mr/1",
            diff_output="",
            test_results=passed_tests_result,
        )
        assert "Aucun fichier modifié détecté" in report

    def test_report_example_toollive_3601(self):
        """Test avec un exemple concret pour TOOLLIVE-3601."""
        spec_content = """# TOOLLIVE-3601: Mise à jour du compteur membres

## Résumé
Mettre à jour l'affichage du nombre de membres de "50 000+" à "90 000+" sur la page /about-us.

## Critères d'acceptation
QUAND un utilisateur visite la page /about-us
ALORS il voit "90 000+ membres" s'afficher correctement

QUAND la page est chargée
ALORS le compteur est visible et lisible

## Test manuel
1. Naviguer vers /about-us
2. Vérifier que le texte "90 000+ membres" est présent
3. Vérifier qu'il n'y a pas d'erreurs d'affichage"""

        diff_output = """diff --git a/src/pages/AboutUs.jsx b/src/pages/AboutUs.jsx
index abc..def 100644
--- a/src/pages/AboutUs.jsx
+++ b/src/pages/AboutUs.jsx
@@ -10,7 +10,7 @@
 <h1>About Us</h1>
 <p>
-We have 50 000+ members
+We have 90 000+ members
 </p>
"""

        test_results = {
            "passed": True,
            "log": "test_about_us.py::test_counter_display PASSED\ntest_about_us.py::test_page_render PASSED"
        }

        report = format_qa_report(
            spec_content=spec_content,
            review_link="https://gitlab.com/toollive/toollive/-/merge_requests/3601",
            diff_output=diff_output,
            test_results=test_results,
        )

        # Vérifications spécifiques
        assert "## 📋 Rapport pour la QA" in report
        assert "50 000+" in report
        assert "90 000+" in report
        assert "https://gitlab.com/toollive/toollive/-/merge_requests/3601" in report
        assert "src/pages/AboutUs.jsx" in report
        assert "QUAND un utilisateur visite la page /about-us" in report
        assert "✅" in report
        assert "Naviguer vers /about-us" in report


# --- Nouveaux tests pour les critères sur une seule ligne ---

class TestExtractAcceptanceCriteriaSingleLine:
    """Tests pour les critères d'acceptation sur une seule ligne."""

    def test_single_line_when_then(self):
        """Critère WHEN/THEN sur une seule ligne sans puces."""
        spec = "WHEN user visits /about-us THEN they see 90 000+ members"
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert "QUAND" in criteria[0]["scenario"]
        assert "ALORS" in criteria[0]["scenario"]

    def test_single_line_dash_when_then(self):
        """Critère WHEN/THEN avec puce -."""
        spec = "- WHEN user visits /about-us THEN they see 90 000+ members"
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert "QUAND" in criteria[0]["scenario"]

    def test_single_line_star_when_then(self):
        """Critère WHEN/THEN avec puce *."""
        spec = "* WHEN user visits /about-us THEN they see 90 000+ members"
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 1

    def test_single_line_bold_when_then(self):
        """Critère WHEN/THEN avec gras **."""
        spec = "- **WHEN** user visits /about-us **THEN** they see 90 000+ members"
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert "QUAND" in criteria[0]["scenario"]
        assert "ALORS" in criteria[0]["scenario"]

    def test_single_line_quand_alors(self):
        """Critère QUAND/ALORS sur une seule ligne."""
        spec = "- **QUAND** l'utilisateur ouvre /about-us **ALORS** il voit « 90 000+ »"
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert "QUAND" in criteria[0]["scenario"]
        assert "ALORS" in criteria[0]["scenario"]

    def test_multiple_single_line_criteria(self):
        """Plusieurs critères sur une seule ligne."""
        spec = """- **WHEN** user visits /about-us **THEN** they see 90 000+
- **WHEN** page loads **THEN** counter is visible"""
        criteria = _extract_acceptance_criteria(spec)
        assert len(criteria) == 2


# --- Nouveaux tests pour les résultats de tests ---

class TestFormatTestResultsNewFormats:
    """Tests pour les nouveaux formats de résultats de tests."""

    def test_pytest_q_format(self):
        """Format pytest -q : '8 passed in 0.5s'."""
        test_results = {"passed": True, "cmd": "pytest -q", "log": "8 passed in 0.5s"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 8 test(s) passé(s)" in results[0]["status"]

    def test_pytest_q_with_failures(self):
        """Format pytest -q avec échecs : '2 failed, 8 passed in 0.5s'."""
        test_results = {"passed": False, "cmd": "pytest -q", "log": "2 failed, 8 passed in 0.5s"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 8 passé(s)" in results[0]["status"]
        assert "❌ 2 échoué(s)" in results[0]["status"]

    def test_jest_format(self):
        """Format Jest : 'Tests: 12 passed, 12 total'."""
        test_results = {"passed": True, "cmd": "npm run test-unit -- --ci", "log": "Tests: 12 passed, 12 total"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 12 test(s) passé(s)" in results[0]["status"]

    def test_jest_format_with_failures(self):
        """Format Jest avec échecs : 'Tests: 10 passed, 2 failed, 12 total'."""
        test_results = {"passed": False, "cmd": "npm run test-unit -- --ci", "log": "Tests: 10 passed, 2 failed, 12 total"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 10 passé(s)" in results[0]["status"]
        assert "❌ 2 échoué(s)" in results[0]["status"]

    def test_passed_failed_counts(self):
        """Utilisation des comptes passed/failed directement."""
        test_results = {"passed": 5, "failed": 2}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 5 passé(s)" in results[0]["status"]
        assert "❌ 2 échoué(s)" in results[0]["status"]

    def test_only_passed_count(self):
        """Seulement des tests passés."""
        test_results = {"passed": 8, "failed": 0}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 8 test(s) passé(s)" in results[0]["status"]

    def test_only_failed_count(self):
        """Seulement des tests échoués."""
        test_results = {"passed": 0, "failed": 3}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "❌ 3 test(s) échoué(s)" in results[0]["status"]

    def test_unrunnable_still_shows_warning(self):
        """Tests non exécutables : doit afficher l'avertissement."""
        test_results = {"unrunnable": True, "cmd": "some command"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "⚠️ Aucun test automatique exécuté" in results[0]["status"]


# --- Test avec des données réalistes ---

    def test_real_jest_format_with_failures(self):
        """Test avec un cas réel Jest : passed=False, log contient le résumé."""
        test_results = {"passed": False, "cmd": "npm run test-unit -- --ci", "log": "Tests: 2 failed, 10 passed, 12 total"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "✅ 10 passé(s)" in results[0]["status"]
        assert "❌ 2 échoué(s)" in results[0]["status"]

    def test_failed_without_summary_shows_error(self):
        """Si passed=False et pas de résumé trouvé, afficher échec."""
        test_results = {"passed": False, "cmd": "npm run test-unit", "log": "Some error output without summary"}
        results = _format_test_results(test_results)
        assert len(results) == 1
        assert "❌ Tests en échec — voir le log de la CI" in results[0]["status"]


class TestRealisticExample:
    """Test avec un exemple réaliste pour TOOLLIVE-3601."""

    def test_realistic_hello_component_example(self):
        """Test avec un SPEC.md et une diff réalistes pour hello.component.html."""
        spec_content = """# TOOLLIVE-3601: Mise à jour du compteur membres

## Résumé
Mettre à jour l'affichage du nombre de membres de "50 000+" à "90 000+" dans le composant hello.

## Critères d'acceptation
- **WHEN** l'utilisateur visite /hello **THEN** il voit "90 000+ membres"
- **QUAND** la page est chargée **ALORS** le compteur est mis à jour

## Test manuel
1. Naviguer vers /hello
2. Vérifier que le texte affiche "90 000+ membres"
"""

        diff_output = """diff --git a/src/app/hello/hello.component.html b/src/app/hello/hello.component.html
index abc123..def456 100644
--- a/src/app/hello/hello.component.html
+++ b/src/app/hello/hello.component.html
@@ -47,7 +47,7 @@
 <div class="counter">
   <span>Nous avons </span>
   <strong>
-   50 000+
+   90 000+
   </strong>
   <span> membres</span>
 </div>
"""

        # Test avec Jest
        test_results_jest = {"passed": True, "cmd": "npm run test-unit -- --ci", "log": "Tests: 12 passed, 12 total"}
        report = format_qa_report(
            spec_content=spec_content,
            review_link="https://gitlab.com/toollive/toollive/-/merge_requests/1590",
            diff_output=diff_output,
            test_results=test_results_jest,
        )

        assert "## 📋 Rapport pour la QA" in report
        assert "Mettre à jour l'affichage du nombre de membres" in report
        assert "https://gitlab.com/toollive/toollive/-/merge_requests/1590" in report
        assert "src/app/hello/hello.component.html" in report
        assert "QUAND l'utilisateur visite /hello ALORS il voit" in report
        assert "✅ 12 test(s) passé(s)" in report
        assert "Naviguer vers /hello" in report

        # Test avec pytest -q
        test_results_pytest = {"passed": True, "cmd": "pytest -q", "log": "8 passed in 0.5s"}
        report = format_qa_report(
            spec_content=spec_content,
            review_link="https://gitlab.com/toollive/toollive/-/merge_requests/1590",
            diff_output=diff_output,
            test_results=test_results_pytest,
        )

        assert "✅ 8 test(s) passé(s)" in report


class TestSummaryPrefersGoal:
    """Constat TOOLLIVE-3603 : le résumé reprenait la section Context (technique)."""

    SPEC_3603 = """# Spec toollive-3603

## Goal
Replace the label "Connected assets" with "Connected tools" on the /about-us page.

## Context
The route about-us is declared in src/app/hello/hello.routes.ts and lazy-loads HelloComponent.

## Acceptance criteria
- WHEN I open /about-us THEN I see "Connected tools"
"""

    def test_goal_avant_context(self):
        summary = _extract_summary(self.SPEC_3603)
        assert summary.startswith('Replace the label "Connected assets"')
        assert "hello.routes.ts" not in summary

    def test_objectif_en_francais(self):
        spec = "# Spec\n\n## Contexte\nDétails techniques.\n\n## Objectif\nAfficher 90 000+ sur /about-us.\n"
        assert _extract_summary(spec).startswith("Afficher 90 000+")

    def test_context_si_pas_de_goal(self):
        spec = "# Spec\n\n## Context\nLa page about-us affiche un compteur.\n"
        assert _extract_summary(spec).startswith("La page about-us")


class TestCriteresSurPlusieursLignes:
    """Constat TOOLLIVE-3604 : critère coupé après « ALORS le texte « … » »."""

    def test_ligne_de_suite_indentee(self):
        spec = (
            "## Acceptance criteria\n"
            "- QUAND j'ouvre la page /about-us ALORS le texte « Partners with a ToolLive account »\n"
            "  est remplacé par « Partners using ToolLive »\n"
            "- QUAND j'ouvre la page /about-us ALORS tout le reste de la page est inchangé\n"
        )
        crit = _extract_acceptance_criteria(spec)
        assert len(crit) == 2
        assert crit[0]["scenario"].endswith("est remplacé par « Partners using ToolLive »")
        assert crit[1]["scenario"].endswith("tout le reste de la page est inchangé")

    def test_pas_de_suite_apres_ligne_vide(self):
        spec = (
            "## Acceptance criteria\n"
            "- WHEN I open /about-us THEN I see the label\n"
            "\n"
            "  Note indentée hors critère\n"
        )
        crit = _extract_acceptance_criteria(spec)
        assert crit[0]["scenario"] == "QUAND I open /about-us ALORS I see the label"


class TestCas3607:
    """Vrai bug TOOLLIVE-3607 : critère principal perdu + étape manuelle générique."""

    SPEC = (
        "## Goal\nCorriger « Quote reopenned by » en « Quote reopened by » (anglais).\n\n"
        "## Acceptance criteria\n"
        "1. QUAND j'ouvre l'historique d'une demande rouverte, en anglais, ALORS je vois « Quote reopened by ».\n"
        "2. Scénario FR : QUAND j'ouvre le même historique en français, ALORS je vois toujours « Devis rouvert par » ;\n"
    )

    def test_criteres_numerotes_et_prefixes(self):
        crit = _extract_acceptance_criteria(self.SPEC)
        assert len(crit) == 2
        assert "je vois « Quote reopened by »" in crit[0]["scenario"]
        assert "Devis rouvert par" in crit[1]["scenario"]

    def test_etapes_manuelles_deduites_des_criteres(self):
        report = format_qa_report(spec_content=self.SPEC, review_link="https://mr/1",
                                  diff_output="", test_results={"passed": True, "cmd": "", "log": ""})
        assert "Vérifier : QUAND j'ouvre l'historique" in report
        assert "Vérifier le fonctionnement général" not in report


def test_format_openspec_when_then_sur_deux_puces():
    """Constat TOOLLIVE-3607 (MR !1595) : le critère principal, au format OpenSpec
    (- **WHEN** … / - **THEN** … / - **AND** …), manquait au rapport."""
    spec = (
        "## Requirements\n"
        "#### Scenario: Historique en anglais\n"
        "- **WHEN** j'ouvre l'historique d'une demande rouverte en anglais\n"
        "- **THEN** je vois « Quote reopened by »\n"
        "- **AND** je ne vois plus « Quote reopenned by »\n\n"
        "#### Scenario: Autres langues\n"
        "- QUAND j'ouvre le même historique en français, ALORS je vois toujours « Devis rouvert par »\n"
    )
    crit = _extract_acceptance_criteria(spec)
    assert len(crit) == 2
    assert crit[0]["scenario"] == ("QUAND j'ouvre l'historique d'une demande rouverte en anglais "
                                   "ALORS je vois « Quote reopened by » ET je ne vois plus « Quote reopenned by »")
    assert "Devis rouvert par" in crit[1]["scenario"]
