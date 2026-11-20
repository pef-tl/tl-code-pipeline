"""Garde-fou du plan (constat TOOLLIVE-3607 : 1 faute -> 5 tâches parallèles)."""

from worker.activities.plan_guard import consolidate_tasks, plan_note

EN = "src/assets/i18n/en.json"


def _t(i, files, deps=()):
    return {"id": i, "title": f"tâche {i}", "capability": "dev",
            "files_hint": list(files), "depends_on": list(deps), "acceptance": f"ok {i}"}


def test_cas_3607_cinq_taches_meme_fichier_deviennent_une():
    tasks = [_t(f"t{i}", [EN]) for i in range(1, 6)]
    out = consolidate_tasks(tasks)
    assert len(out) == 1
    assert out[0]["id"] == "t1+t2+t3+t4+t5"
    assert out[0]["files_hint"] == [EN]
    assert out[0]["depends_on"] == []
    assert "ℹ️ Plan : 5 tâches regroupées en 1" in plan_note(tasks, out)


def test_taches_independantes_inchangees():
    tasks = [_t("a", ["x.ts"]), _t("b", ["y.ts"])]
    assert consolidate_tasks(tasks) == tasks
    assert plan_note(tasks, tasks) == ""


def test_regroupement_transitif_et_dependances_redirigees():
    tasks = [_t("a", ["x.ts"]), _t("b", ["x.ts", "y.ts"]), _t("c", ["y.ts"]), _t("d", ["z.ts"], deps=["b"])]
    out = consolidate_tasks(tasks)
    assert [t["id"] for t in out] == ["a+b+c", "d"]
    assert out[0]["files_hint"] == ["x.ts", "y.ts"]
    assert out[1]["depends_on"] == ["a+b+c"]


def test_dependance_interne_supprimee():
    tasks = [_t("a", ["x.ts"]), _t("b", ["./x.ts"], deps=["a"])]
    out = consolidate_tasks(tasks)
    assert len(out) == 1 and out[0]["depends_on"] == []


def test_taches_sans_fichier_non_regroupees():
    tasks = [_t("a", []), _t("b", [])]
    assert consolidate_tasks(tasks) == tasks


def test_alerte_plus_de_3_taches():
    tasks = [_t(c, [f"{c}.ts"]) for c in "abcd"]
    assert "⚠️ Plan en 4 tâches" in plan_note(tasks, tasks)


def test_plan_vide():
    assert consolidate_tasks([]) == []
