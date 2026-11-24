"""Workflow JIRA TOOLLIVE v4 : statuts, transitions autorisées et gardes.

Module PUR (aucun import SDK, aucun réseau) : la garde côté code s'applique même
tant que le workflow JIRA v3 (« N'importe lequel » partout) est en place. Avant
chaque changement de statut, le pipeline vérifie que la transition figure dans
ALLOWED et qu'elle n'est pas réservée à un humain, sinon il ne touche pas au
ticket (non bloquant : la raison est remontée dans Studio et dans le « Suivi »).

Statuts v4 (16) : CONCEPTION est fusionné dans SPECS, TEST et TESTS MANUELS dans
TO-BE-TESTED. Les noms sont comparés normalisés (casse, espaces, tirets, « _ »
ignorés) : « devs-en-cours » == « DEVS EN COURS ».

Nom réel de chaque statut côté pipeline : env `jira_status_<phase>` (minuscules,
fallback majuscules), défaut = nom du workflow v4. Les anciennes variables
`jira_transition_start` / `jira_transition_review` / `jira_transition_done`
restent prises en compte pour les phases start / review / done.
"""

from __future__ import annotations

import os
import re

# ------------------------------------------------------------------ statuts ---
NOUVEAU = "Nouveau"
A_FAIRE = "A faire"
PRIS_EN_CHARGE = "Pris en charge"
SPECS = "Specs"
READY_FOR_DEV = "ready-for-dev"
DEVS_EN_COURS = "devs-en-cours"
TESTS_AUTO = "Tests auto"
CODE_REVIEW = "code-review"
CODE_REVIEW_IN_PROGRESS = "code-review-in-progress"
TO_BE_TESTED = "to-be-tested"
TESTED_KO = "tested-ko"
READY_FOR_VAL = "ready-for-val"
READY_FOR_PROD = "ready-for-prod"
TERMINE = "Terminé"
ARCHIVE = "Archive"
BLOCKED = "blocked"

# Statuts « en cours » : depuis chacun, passage en BLOCKED autorisé (motif requis).
IN_PROGRESS = (PRIS_EN_CHARGE, SPECS, READY_FOR_DEV, DEVS_EN_COURS, TESTS_AUTO,
               CODE_REVIEW, CODE_REVIEW_IN_PROGRESS)

# Transitions du workflow v4 : (de, vers, réservée à un humain ?).
# Numérotation = tableau de la proposition v4.
TRANSITIONS: tuple[tuple[str, str, bool], ...] = (
    (NOUVEAU, A_FAIRE, True),                          # 1
    (A_FAIRE, PRIS_EN_CHARGE, False),                  # 2
    (PRIS_EN_CHARGE, SPECS, False),                    # 3
    (SPECS, READY_FOR_DEV, True),                      # 4  gate n°1
    (SPECS, PRIS_EN_CHARGE, True),                     # 5  spec rejetée
    (READY_FOR_DEV, DEVS_EN_COURS, False),             # 6
    (DEVS_EN_COURS, TESTS_AUTO, False),                # 7
    (TESTS_AUTO, DEVS_EN_COURS, False),                # 8  tests KO (< 3 essais)
    (TESTS_AUTO, CODE_REVIEW, False),                  # 9  lien MR renseigné
    (CODE_REVIEW, CODE_REVIEW_IN_PROGRESS, True),      # 10 retour de review
    (CODE_REVIEW_IN_PROGRESS, CODE_REVIEW, False),     # 11 MR re-poussée
    (CODE_REVIEW, TO_BE_TESTED, True),                 # 12 gate n°2
    (TO_BE_TESTED, TESTED_KO, True),                   # 13
    (TESTED_KO, DEVS_EN_COURS, False),                 # 14
    (TO_BE_TESTED, READY_FOR_VAL, True),               # 15
    (READY_FOR_VAL, READY_FOR_PROD, True),             # 16
    (READY_FOR_PROD, TERMINE, True),                   # 17
    (TERMINE, ARCHIVE, True),                          # 18
) + tuple((s, BLOCKED, False) for s in IN_PROGRESS)    # 19
# 20 : BLOCKED -> statut précédent, humain seulement (voir check_transition).


# Noms internes renvoyés par l'API JIRA pour certains statuts dont l'écran
# affiche le nom traduit (constat TOOLLIVE-3621 : « Nouveau » lu « new » via le
# connecteur -> garde « new » → « Pris en charge » refusée).
_ALIASES = {"new": "nouveau", "open": "nouveau", "todo": "afaire", "done": "termine",
            "terminé": "termine"}


def norm(name: object) -> str:
    """« In Progress », « in-progress », « IN_PROGRESS » -> « inprogress » ;
    noms internes JIRA ramenés au statut v4 (« new » -> « nouveau »)."""
    key = re.sub(r"[\s_\-]+", "", str(name or "")).lower()
    return _ALIASES.get(key, key)


_EDGES: dict[tuple[str, str], bool] = {(norm(a), norm(b)): human for a, b, human in TRANSITIONS}
_KNOWN = {norm(a) for a, _, _ in TRANSITIONS} | {norm(b) for _, b, _ in TRANSITIONS}


def check_transition(current: str, target: str, by_human: bool = False) -> tuple[bool, str]:
    """Garde : (autorisée ?, raison).

    `by_human=True` quand le pipeline exécute la décision explicite d'un humain
    (signal approve/reject/reply reçu juste avant) : les transitions « humain
    seulement » sont alors permises. Statut actuel inconnu ou vide : seules les
    transitions non réservées vers un statut v4 passent (le pipeline ne devine
    pas d'où vient le ticket pour une décision humaine)."""
    cur, tgt = norm(current), norm(target)
    if not tgt:
        return False, "statut cible vide"
    if tgt not in _KNOWN:
        return False, f"« {target} » n'est pas un statut du workflow v4"
    if cur == tgt:
        return True, "déjà dans ce statut"
    if cur == norm(BLOCKED):
        # 20 : sortie de BLOCKED = blocage levé par un humain uniquement.
        if by_human:
            return True, "blocage levé par un humain"
        return False, "sortie de « blocked » réservée à un humain"
    if not cur:
        human_only = any(h for (a, b), h in _EDGES.items() if b == tgt)
        if human_only and not by_human:
            return False, f"statut actuel inconnu et « {target} » est une décision humaine"
        return True, "statut actuel inconnu"
    if (cur, tgt) not in _EDGES:
        via = path_to(current, target, by_human)
        if via:
            return True, "autorisée via " + " → ".join(via)
        return False, f"transition « {current} » → « {target} » absente du workflow v4"
    if _EDGES[(cur, tgt)] and not by_human:
        return False, f"« {current} » → « {target} » est réservée à un humain"
    return True, "autorisée"


def path_to(current: str, target: str, by_human: bool = False, max_steps: int = 2) -> list[str]:
    """Plus court chemin de statuts autorisés de `current` à `target`, départ
    exclu (ex. « Specs » -> « devs-en-cours » après la gate n°1 =
    [ready-for-dev, devs-en-cours]). [] si déjà dessus OU si aucun chemin
    autorisé n'existe (distinguer avec norm(current) == norm(target)).
    Ne passe jamais par « blocked » sauf si c'est la cible. Au plus
    `max_steps` étapes : rattraper un cran (gate humaine + reprise), jamais
    sauter une phase entière du workflow."""
    cur, tgt = norm(current), norm(target)
    if cur == tgt:
        return []
    names = {norm(a): a for a, _, _ in TRANSITIONS} | {norm(b): b for _, b, _ in TRANSITIONS}
    frontier, seen = [(cur, [])], {cur}
    while frontier and len(frontier[0][1]) < max_steps:
        nxt = []
        for node, path in frontier:
            for (a, b), human in _EDGES.items():
                if a != node or b in seen or (human and not by_human):
                    continue
                if b == norm(BLOCKED) and b != tgt:
                    continue
                if b == tgt:
                    return path + [names[b]]
                seen.add(b)
                nxt.append((b, path + [names[b]]))
        frontier = nxt
    return []


# ------------------------------------------- phases du pipeline -> statuts ---
# Phase du pipeline -> (variables d'env, statut v4 par défaut).
PHASES: dict[str, tuple[tuple[str, ...], str]] = {
    "start": (("jira_status_start", "jira_transition_start"), PRIS_EN_CHARGE),
    "spec": (("jira_status_spec",), SPECS),
    "spec_approved": (("jira_status_spec_approved",), READY_FOR_DEV),
    "spec_rejected": (("jira_status_spec_rejected",), PRIS_EN_CHARGE),
    "dev": (("jira_status_dev",), DEVS_EN_COURS),
    "tests": (("jira_status_tests",), TESTS_AUTO),
    "review": (("jira_status_review", "jira_transition_review"), CODE_REVIEW),
    "review_fix": (("jira_status_review_fix",), CODE_REVIEW_IN_PROGRESS),
    # Décision n°3 de la proposition v4 (à trancher) : vide par défaut = le
    # pipeline ne touche pas au ticket après la gate n°2.
    "done": (("jira_status_done", "jira_transition_done"), ""),
    "blocked": (("jira_status_blocked",), BLOCKED),
}


def status_for(phase: str) -> str:
    """Nom du statut JIRA visé pour une phase (env d'abord, puis défaut v4)."""
    names, default = PHASES[phase]
    for name in names:
        for key in (name, name.upper()):
            value = os.environ.get(key)
            if value is not None:
                return value.strip()
    return default


def guard_enabled() -> bool:
    """Garde active par défaut ; `jira_transition_guard=off` la désactive (repli
    d'urgence si un statut réel ne correspond pas au tableau v4)."""
    value = os.environ.get("jira_transition_guard", os.environ.get("JIRA_TRANSITION_GUARD", "on"))
    return value.strip().lower() not in ("off", "0", "false", "non", "no")
