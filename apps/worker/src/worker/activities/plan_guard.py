"""Garde-fou sur le plan du planner (fonctions pures, appelables depuis le
workflow).

Constat TOOLLIVE-3607 (exécution 2a381e2a) : pour UNE faute d'orthographe dans
la traduction anglaise, le planner a créé 5 tâches parallèles (5 agents dev,
5 run_tests de 5 min) qui touchaient les mêmes fichiers ; l'exécution est
ensuite restée bloquée sans erreur. Deux tâches qui modifient le même fichier
en parallèle produisent de toute façon des conflits de fusion : on les regroupe
en UNE tâche.
"""

from __future__ import annotations

MAX_PARALLEL_TASKS = 3


def _norm(hint: str) -> str:
    return str(hint).strip().lstrip("./").rstrip("/")


def consolidate_tasks(tasks: list[dict]) -> list[dict]:
    """Regroupe les tâches qui partagent au moins un fichier (`files_hint`).

    Un groupe devient une seule tâche : id/titre/acceptance concaténés, union
    des fichiers, dépendances externes conservées (les dépendances internes au
    groupe disparaissent, celles des autres tâches vers un membre du groupe sont
    redirigées vers la tâche fusionnée). Les tâches sans fichier commun restent
    telles quelles. L'ordre d'origine est conservé.
    """
    if not tasks:
        return tasks
    n = len(tasks)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, task in enumerate(tasks):
        for hint in task.get("files_hint") or []:
            key = _norm(hint)
            if not key:
                continue
            if key in owner:
                parent[find(i)] = find(owner[key])
            else:
                owner[key] = i

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    if len(groups) == n:
        return tasks  # rien à regrouper

    new_id: dict[str, str] = {}
    merged: list[tuple[int, dict]] = []
    for members in groups.values():
        items = [tasks[i] for i in members]
        if len(items) == 1:
            merged.append((members[0], dict(items[0])))
            continue
        ids = [str(t["id"]) for t in items]
        mid = "+".join(ids)
        for old in ids:
            new_id[old] = mid
        files: list[str] = []
        for t in items:
            for h in t.get("files_hint") or []:
                if h not in files:
                    files.append(h)
        deps: list[str] = []
        for t in items:
            for d in t.get("depends_on") or []:
                if str(d) not in ids and d not in deps:
                    deps.append(d)
        merged.append((members[0], {
            "id": mid,
            "title": " ; ".join(str(t.get("title", "")) for t in items),
            "capability": items[0].get("capability", ""),
            "files_hint": files,
            "depends_on": deps,
            "acceptance": " ; ".join(str(t.get("acceptance", "")) for t in items if t.get("acceptance")),
        }))

    merged.sort(key=lambda pair: pair[0])
    out = []
    for _, task in merged:
        deps = []
        for d in task.get("depends_on") or []:
            d2 = new_id.get(str(d), d)
            if d2 != task["id"] and d2 not in deps:
                deps.append(d2)
        task["depends_on"] = deps
        out.append(task)
    return out


def plan_note(original: list[dict], final: list[dict]) -> str:
    """Note lisible sur le plan (vide si rien à signaler)."""
    lines = []
    if len(final) < len(original):
        lines.append(f"ℹ️ Plan : {len(original)} tâches regroupées en {len(final)} "
                     "(elles modifiaient les mêmes fichiers).")
    if len(final) > MAX_PARALLEL_TASKS:
        lines.append(f"⚠️ Plan en {len(final)} tâches pour ce ticket — vérifier qu'il n'est "
                     "pas découpé plus que nécessaire.")
    return "\n".join(lines)
