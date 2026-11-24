"""Génération du rapport QA pour les commentaires Jira.

Ce module fournit une fonction pour formater un rapport structuré
pour l'équipe QA, incluant : résumé, fichiers modifiés, critères d'acceptation,
résultats des tests et instructions de test manuel.
"""

import re
from typing import Any


GENERIC_STEP = "Vérifier le fonctionnement général de la fonctionnalité modifiée"


def _extract_summary(spec_content: str) -> str:
    """Extrait un résumé de 1-2 phrases depuis SPEC.md.
    
    Cherche une section 'Résumé', 'Summary', ou utilise les premières lignes.
    """
    if not spec_content:
        return "Aucun résumé disponible"
    
    # Chercher d'abord le BUT du changement (section Goal de SPEC.md), puis un
    # résumé, et seulement en dernier le contexte technique (constat TOOLLIVE-3603 :
    # le résumé reprenait « Context » — routes, fichiers — au lieu du but).
    # Plusieurs patterns par niveau : titre de section (## Goal) puis « Goal: ».
    patterns = []
    for names in ("Goal|Goals|Objectif|Objectifs|But|Objective",
                  "Résumé|Summary",
                  "Contexte|Context"):
        patterns.append(rf"#{{1,4}}\s*(?:{names})\s*\n+(.+?)(?=\n\s*\n|\n#|\Z)")
        patterns.append(rf"(?:^|\n)\s*\**(?:{names})\**\s*:\s*(.+?)(?=\n\n|\n#|\Z)")
    patterns.append(r"^#.*?\n\n(.+?)(?=\n\n|\n#|\n##|\Z)")
    
    for pattern in patterns:
        match = re.search(pattern, spec_content, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
            # Nettoyer le texte : enlever les marqueurs markdown
            text = re.sub(r"^#+\s*", "", text)
            text = re.sub(r"^[-*•]\s*", "", text)
            # Garder seulement 1-2 phrases
            sentences = re.split(r"[.!?]\s+", text)
            result = ". ".join(sentences[:2]).strip()
            if len(sentences) > 0 and not text.endswith((".", "!", "?")):
                result += "."
            return result
    
    # Sinon, prendre les premières lignes
    lines = spec_content.split("\n")
    non_empty_lines = [l.strip() for l in lines if l.strip()]
    if non_empty_lines:
        # Prendre les 2 premières lignes non vides
        summary = " ".join(non_empty_lines[:2])
        # Tronquer à 200 caractères max
        return summary[:200].strip() + "..." if len(summary) > 200 else summary.strip()
    
    return "Aucun résumé disponible"


def _extract_acceptance_criteria(spec_content: str) -> list[dict]:
    """Extrait les critères d'acceptation sous forme de scénarios QUAND/ALORS ou WHEN/THEN.
    
    Gère :
    - Scénarios sur plusieurs lignes (QUAND...\nALORS...)
    - Scénarios sur une seule ligne (- **WHEN** ... **THEN** ...)
    - Puces (-, *, •)
    - Gras (**)
    - WHEN/THEN ou QUAND/ALORS
    
    Retourne une liste de dicts avec 'id', 'scenario', 'status' ('non testé').
    """
    criteria = []
    if not spec_content:
        return criteria
    
    # Chercher une section dédiée (## ou ###)
    section_pattern = r"(?:#{2,3}\s*(?:Critères? d'(?:acceptation|accéptation)|Acceptance Criteria?|Scénarios?)\s*\n+)(.+?)(?=\n\n#|\Z)"
    section_match = re.search(section_pattern, spec_content, re.DOTALL | re.IGNORECASE)
    
    if section_match:
        content = section_match.group(1)
    else:
        content = spec_content
    
    # Pattern 1: Scénarios sur une seule ligne avec gras **WHEN**...**THEN**
    # Ex: - **WHEN** x **THEN** y
    bold_single_line = r"^\s*[-*•]\s*\*\*(?:WHEN|QUAND)\*\*(.+?)\*\*(?:THEN|ALORS)\*\*(.+?)\s*$"
    
    # Pattern 2: Scénarios sur une seule ligne sans gras mais avec WHEN/THEN
    # Ex: - WHEN x THEN y
    plain_single_line = r"^\s*[-*•]\s*(?:WHEN|QUAND)\s+(.+?)\s+(?:THEN|ALORS)\s+(.+?)\s*$"
    
    # Pattern 2b: Scénarios sur une seule ligne sans puce
    # Ex: WHEN x THEN y
    no_bullet_single_line = r"^\s*(?:WHEN|QUAND)\s+(.+?)\s+(?:THEN|ALORS)\s+(.+?)\s*$"
    
    # Pattern 3: Scénarios sur plusieurs lignes
    multi_line_pattern = r"(?:^|\n)\s*(?:QUAND|WHEN)\s+(.+?)\s*\n\s*(?:ALORS|THEN)\s+(.+?)\s*(?=\n|$)"
    
    # Extraire les scénarios sur une seule ligne avec gras
    last = None  # index du dernier critère trouvé, pour les lignes de suite
    pending_when = None  # WHEN seul sur sa ligne, en attente du THEN
    for line in content.split("\n"):
        # Ligne de suite d'un critère coupé sur plusieurs lignes (constat
        # TOOLLIVE-3604 : « ALORS le texte « … » » sans la fin) : ligne indentée,
        # ni puce, ni titre, ni nouveau QUAND/WHEN.
        if (last is not None and line[:1] in (" ", "\t") and line.strip()
                and not re.match(r"\s*([-*•]|#|\**(WHEN|QUAND|THEN|ALORS)\b)", line, re.IGNORECASE)):
            criteria[last]["scenario"] += " " + line.strip()
            continue
        only_and = re.match(r"^\s*(?:[-*•])?\s*\**(?:AND|ET)\b\**\s+(.+?)\s*$", line, re.IGNORECASE)
        if only_and and last is not None:
            criteria[last]["scenario"] += f" ET {only_and.group(1).strip()}"
            continue
        if not line.strip() or re.match(r"\s*([-*•]|#)", line):
            last = None
        # Format OpenSpec : « - **WHEN** … » puis, ligne suivante, « - **THEN** … »
        # (et éventuellement « - **AND** … ») — constat TOOLLIVE-3607 : le critère
        # principal, écrit ainsi, manquait au rapport.
        only_when = re.match(r"^\s*(?:[-*•]|\d+[.)])?\s*\**(?:WHEN|QUAND)\b\**\s+(.+?)\s*$", line, re.IGNORECASE)
        if only_when and not re.search(r"\b(?:THEN|ALORS)\b", line, re.IGNORECASE):
            pending_when = only_when.group(1).strip()
            continue
        only_then = re.match(r"^\s*(?:[-*•]|\d+[.)])?\s*\**(?:THEN|ALORS)\b\**\s+(.+?)\s*$", line, re.IGNORECASE)
        if only_then and pending_when:
            criteria.append({"id": len(criteria) + 1, "status": "non testé",
                             "scenario": f"QUAND {pending_when} ALORS {only_then.group(1).strip()}"})
            last = len(criteria) - 1
            pending_when = None
            continue
        line_match = re.match(bold_single_line, line, re.IGNORECASE | re.MULTILINE)
        if line_match:
            when_part = line_match.group(1).strip()
            then_part = line_match.group(2).strip()
            scenario_text = f"QUAND {when_part} ALORS {then_part}"
            criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
            last = len(criteria) - 1
            continue
        
        # Extraire les scénarios sur une seule ligne sans gras
        line_match = re.match(plain_single_line, line, re.IGNORECASE | re.MULTILINE)
        if line_match:
            when_part = line_match.group(1).strip()
            then_part = line_match.group(2).strip()
            scenario_text = f"QUAND {when_part} ALORS {then_part}"
            criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
            last = len(criteria) - 1
            continue
        
        # Extraire les scénarios sur une seule ligne sans puce
        line_match = re.match(no_bullet_single_line, line, re.IGNORECASE | re.MULTILINE)
        if line_match:
            when_part = line_match.group(1).strip()
            then_part = line_match.group(2).strip()
            scenario_text = f"QUAND {when_part} ALORS {then_part}"
            criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
            last = len(criteria) - 1
            continue

        # Dernier recours : QUAND/WHEN … ALORS/THEN n'importe où dans la ligne
        # (listes numérotées « 1. », préfixes « Scénario 1 : », gras partiel…).
        line_match = re.search(
            r"\**\b(?:WHEN|QUAND)\b\**\s+(.+?)\s*,?\s*\**\b(?:THEN|ALORS)\b\**\s+(.+?)\s*$",
            line, re.IGNORECASE)
        if line_match:
            scenario_text = f"QUAND {line_match.group(1).strip()} ALORS {line_match.group(2).strip()}"
            criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
            last = len(criteria) - 1
            continue
    
    # Extraire les scénarios sur plusieurs lignes
    multi_line_scenarios = re.findall(multi_line_pattern, content, re.DOTALL | re.IGNORECASE | re.MULTILINE)
    for when, then in multi_line_scenarios:
        scenario_text = f"QUAND {when.strip()} ALORS {then.strip()}"
        criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
    
    # Si toujours rien, essayer de trouver des paires WHEN/THEN ou QUAND/ALORS séparées
    if not criteria:
        lines_list = content.split("\n")
        current_when = None
        for line in lines_list:
            line = line.strip()
            # Enlever les marqueurs markdown
            line_clean = re.sub(r"^[-*•]\s*", "", line)
            line_clean = re.sub(r"\*\*", "", line_clean)
            
            when_match = re.match(r"(?:QUAND|WHEN)\s+(.+)", line_clean, re.IGNORECASE)
            if when_match:
                current_when = when_match.group(1).strip()
                continue
            then_match = re.match(r"(?:ALORS|THEN)\s+(.+)", line_clean, re.IGNORECASE)
            if then_match and current_when:
                scenario_text = f"QUAND {current_when} ALORS {then_match.group(1).strip()}"
                criteria.append({"id": len(criteria) + 1, "scenario": scenario_text, "status": "non testé"})
                current_when = None
    
    return criteria

def _extract_manual_test_steps(spec_content: str) -> list[str]:
    """Extrait les étapes de test manuel depuis SPEC.md.
    
    Cherche une section 'Test manuel', 'Manual testing', etc.
    """
    steps = []
    if not spec_content:
        return [GENERIC_STEP]
    
    patterns = [
        r"(?:Test manuel|Manual testing|Comment tester|How to test):\s*\n+(.+?)(?=\n\n#|\Z)",
        r"#{2,3}\s*(?:Test manuel|Manual testing|Comment tester|How to test)\s*\n+(.+?)(?=\n\n#|\Z)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, spec_content, re.DOTALL | re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            # Extraire les étapes (lignes commençant par - , *, •, ou numéro)
            step_lines = re.findall(r"^\s*[-*•]\s+(.+?)$|^\s*[0-9]+[.)]\s+(.+?)$", content, re.MULTILINE)
            for step_tuple in step_lines:
                step = step_tuple[0] or step_tuple[1]
                if step.strip():
                    steps.append(step.strip())
            if steps:
                return steps
    
    # Si pas trouvé, retourner une instruction générique
    return [GENERIC_STEP]


def _extract_modified_files(diff_output: str) -> list[str]:
    """Extrait la liste des fichiers modifiés depuis un diff git."""
    if not diff_output:
        return []
    
    files = set()
    # Format diff: diff --git a/file1 b/file2
    for line in diff_output.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                # a/file et b/file
                old_file = parts[2][2:]  # enlève 'a/'
                new_file = parts[3][2:]  # enlève 'b/'
                # Prendre le nouveau nom (pour les renommages)
                if old_file != new_file:
                    files.add(new_file)
                else:
                    files.add(old_file)
    
    return sorted(list(files))


def _format_test_results(test_results: dict) -> list[dict]:
    """Formate les résultats des tests pour le rapport.
    
    Utilise test_results["passed"] et parse le log pour Jest et pytest -q.
    Affiche un résumé (nombre de tests passés/échoués) au lieu de lister chaque test.
    
    test_results est le dict retourné par test_runner.run_tests() :
    {"passed": bool, "cmd": str, "log": str}
    """
    # Cas 1: Aucun test exécuté
    if not test_results:
        return [{"status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"}]
    
    # Cas 2: Tests non exécutables
    if test_results.get("unrunnable"):
        return [{"status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"}]
    
    # Cas 3: Utiliser passed/failed si disponibles (et que ce sont des entiers)
    passed_count = test_results.get("passed")
    failed_count = test_results.get("failed")
    
    # Si passed et failed sont des entiers (pas des booléens), les utiliser
    if isinstance(passed_count, int) and isinstance(failed_count, int):
        total = passed_count + failed_count
        if total > 0:
            if failed_count == 0:
                return [{"status": f"✅ {total} test(s) passé(s)"}]
            elif passed_count == 0:
                return [{"status": f"❌ {total} test(s) échoué(s)"}]
            else:
                return [{"status": f"✅ {passed_count} passé(s), ❌ {failed_count} échoué(s)"}]
    
    # Cas 4: Parser le log pour pytest standard et pytest -q
    log = test_results.get("log", "")
    if log:
        # pytest -q: "8 passed in 0.5s" ou "2 failed, 8 passed in 0.5s"
        pytest_q_match = re.search(r"(\d+)\s*passed", log)
        pytest_q_fail_match = re.search(r"(\d+)\s*failed", log)
        if pytest_q_match:
            passed = int(pytest_q_match.group(1))
            failed = int(pytest_q_fail_match.group(1)) if pytest_q_fail_match else 0
            total = passed + failed
            if total > 0:
                if failed == 0:
                    return [{"status": f"✅ {total} test(s) passé(s)"}]
                elif passed == 0:
                    return [{"status": f"❌ {total} test(s) échoué(s)"}]
                else:
                    return [{"status": f"✅ {passed} passé(s), ❌ {failed} échoué(s)"}]
        
        # Jest: "Tests: 12 passed, 12 total" ou "Tests: 10 passed, 2 failed, 12 total"
        jest_match = re.search(r"Tests:\s*(\d+)\s*passed,\s*(\d+)\s*total", log)
        if jest_match:
            passed = int(jest_match.group(1))
            total = int(jest_match.group(2))
            failed = total - passed
            if failed == 0:
                return [{"status": f"✅ {total} test(s) passé(s)"}]
            else:
                return [{"status": f"✅ {passed} passé(s), ❌ {failed} échoué(s)"}]
        
        # Jest avec échecs: "Tests: 10 passed, 2 failed, 12 total"
        jest_fail_match = re.search(r"Tests:\s*(\d+)\s*passed,\s*(\d+)\s*failed,\s*(\d+)\s*total", log)
        if jest_fail_match:
            passed = int(jest_fail_match.group(1))
            failed = int(jest_fail_match.group(2))
            return [{"status": f"✅ {passed} passé(s), ❌ {failed} échoué(s)"}]
        
        # pytest standard: parser les lignes PASSED/FAILED
        passed_tests = 0
        failed_tests = 0
        for line in log.split("\n"):
            if " PASSED" in line and "test_" in line:
                passed_tests += 1
            elif " FAILED" in line and "test_" in line:
                failed_tests += 1
        
        total = passed_tests + failed_tests
        if total > 0:
            if failed_tests == 0:
                return [{"status": f"✅ {total} test(s) passé(s)"}]
            elif passed_tests == 0:
                return [{"status": f"❌ {total} test(s) échoué(s)"}]
            else:
                return [{"status": f"✅ {passed_tests} passé(s), ❌ {failed_tests} échoué(s)"}]
    
    # Cas 5: Si passed est False et qu'on n'a pas trouvé de résumé, afficher échec
    if test_results.get("passed") is False:
        return [{"status": "❌ Tests en échec — voir le log de la CI"}]
    
    # Cas 6: Aucun test trouvé
    return [{"status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"}]



def format_qa_report(
    spec_content: str,
    review_link: str,
    diff_output: str,
    test_results: dict,
) -> str:
    """Génère un rapport QA structuré pour Jira.
    
    Args:
        spec_content: Contenu de SPEC.md
        review_link: URL de la MR
        diff_output: Output de git diff
        test_results: Dict des résultats des tests (de test_runner.run_tests)
    
    Returns:
        str: Commentaire Jira formaté en markdown
    """
    # Extraire les informations
    summary = _extract_summary(spec_content)
    criteria = _extract_acceptance_criteria(spec_content)
    modified_files = _extract_modified_files(diff_output)
    test_results_formatted = _format_test_results(test_results)
    manual_steps = _extract_manual_test_steps(spec_content)
    if manual_steps == [GENERIC_STEP] and criteria:
        # Pas de section « test manuel » dans la spec : on déduit les étapes des
        # critères (constat TOOLLIVE-3606/3607 : étape générique inutile pour la QA).
        manual_steps = [f"Vérifier : {c['scenario']}" for c in criteria]
        manual_steps.append("Vérifier qu'aucun autre texte ni élément de la page n'a changé.")
    
    # Construire le rapport
    lines = []
    lines.append("## 📋 Rapport pour la QA")
    lines.append("")
    
    # 1. Résumé
    lines.append("**Résumé** : " + summary)
    lines.append("")
    
    # 2. Lien MR
    lines.append(f"**MR** : {review_link}")
    lines.append("")
    
    # 3. Fichiers modifiés
    lines.append("**Fichiers modifiés** :")
    if modified_files:
        for f in modified_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("- Aucun fichier modifié détecté")
    lines.append("")
    
    # 4. Critères d'acceptation
    lines.append("**Critères d'acceptation** :")
    if criteria:
        for crit in criteria:
            lines.append(f"- [{crit['status']}] {crit['scenario']}")
    else:
        lines.append("- Aucun critère d'acceptation trouvé dans SPEC.md")
    lines.append("")
    
    # 5. Tests automatiques
    lines.append("**Tests automatiques** :")
    if test_results_formatted:
        for test in test_results_formatted:
            # Si test a un 'name', utiliser l'ancien format, sinon juste le status
            if 'name' in test:
                lines.append(f"- {test['name']} : {test['status']}")
            else:
                lines.append(f"- {test['status']}")
    else:
        lines.append("- ⚠️ Aucun test automatique exécuté — test manuel obligatoire")
    lines.append("")
    
    # 6. Test manuel
    lines.append("**Comment tester à la main** :")
    if manual_steps:
        for i, step in enumerate(manual_steps, 1):
            lines.append(f"{i}. {step}")
    else:
        lines.append("1. Vérifier le fonctionnement général de la fonctionnalité modifiée")
    
    return "\n".join(lines)
