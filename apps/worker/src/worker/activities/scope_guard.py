"""Garde-fou « hors sujet » : repère les fichiers modifiés qui ne font pas
partie du plan (champ `files_hint` des tâches du planner).

Fonctions pures (pas d'I/O) : appelables depuis le workflow (sandbox Temporal).
Constat d'origine : exécution TOOLLIVE-3601 (MR !1590) — le ticket demandait
seulement `50 000+` -> `90 000+`, mais la MR contenait aussi SPEC.md et un
changement d'apostrophe non demandé.
"""

from __future__ import annotations

import fnmatch

# Fichiers du pipeline qui ne doivent jamais arriver dans le repo cible.
PIPELINE_FILES = ("SPEC.md",)


def modified_files(diff_output: str) -> list[str]:
    """Fichiers modifiés d'après une sortie `git diff` (lignes `diff --git a/x b/y`)."""
    files: set[str] = set()
    for line in (diff_output or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                files.add(parts[3][2:])
    return sorted(files)


def plan_hints(tasks: list[dict] | None) -> list[str]:
    """Union des `files_hint` de toutes les tâches du plan."""
    hints: list[str] = []
    for task in tasks or []:
        for hint in task.get("files_hint") or []:
            hint = str(hint).strip().lstrip("./")
            if hint and hint not in hints:
                hints.append(hint)
    return hints


def _matches(path: str, hint: str) -> bool:
    if path == hint:
        return True
    if any(c in hint for c in "*?["):
        return fnmatch.fnmatch(path, hint)
    # un hint de dossier couvre son contenu
    return path.startswith(hint.rstrip("/") + "/")


def out_of_scope(files: list[str], hints: list[str]) -> list[str]:
    """Fichiers modifiés hors plan. Les fichiers du pipeline (SPEC.md) sont
    toujours signalés. Sans aucun hint, on ne peut rien conclure : seuls les
    fichiers du pipeline sont signalés."""
    flagged = [f for f in files if f.split("/")[-1] in PIPELINE_FILES]
    if hints:
        flagged += [f for f in files
                    if f not in flagged and not any(_matches(f, h) for h in hints)]
    return flagged


def scope_note(files: list[str], hints: list[str]) -> str:
    """Texte lisible (Jira / gate de review) ; vide si tout est dans le plan."""
    extra = out_of_scope(files, hints)
    if not extra:
        return ""
    lines = ["⚠️ Fichiers modifiés hors du plan — à vérifier en review :"]
    lines += [f"- `{f}`" for f in extra]
    return "\n".join(lines)
