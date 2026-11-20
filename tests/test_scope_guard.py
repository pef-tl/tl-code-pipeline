"""Garde-fou hors sujet + SPEC.md jamais committé (constat TOOLLIVE-3601 / MR !1590).

Run : uv run --with pytest python -m pytest tests/ -q
"""

import asyncio
import subprocess

import pytest

from worker.activities import git_tools
from worker.activities.scope_guard import (
    modified_files,
    out_of_scope,
    plan_hints,
    scope_note,
)

DIFF_1590 = """diff --git a/src/app/hello/hello.component.html b/src/app/hello/hello.component.html
index 1..2 100644
--- a/src/app/hello/hello.component.html
+++ b/src/app/hello/hello.component.html
@@ -94 +94 @@
-<div>50 000+</div>
+<div>90 000+</div>
diff --git a/SPEC.md b/SPEC.md
new file mode 100644
--- /dev/null
+++ b/SPEC.md
@@ -0,0 +1 @@
+# Spec
"""

TASKS = [{"id": "t1", "files_hint": ["src/app/hello/hello.component.html"]}]


def test_modified_files_depuis_la_diff():
    assert modified_files(DIFF_1590) == ["SPEC.md", "src/app/hello/hello.component.html"]
    assert modified_files("") == []


def test_plan_hints_union_sans_doublon():
    tasks = [{"files_hint": ["a.ts", "./b/"]}, {"files_hint": ["a.ts"]}, {}]
    assert plan_hints(tasks) == ["a.ts", "b/"]
    assert plan_hints(None) == []


def test_cas_reel_1590_spec_md_signale():
    files = modified_files(DIFF_1590)
    assert out_of_scope(files, plan_hints(TASKS)) == ["SPEC.md"]
    note = scope_note(files, plan_hints(TASKS))
    assert "hors du plan" in note and "`SPEC.md`" in note
    assert "hello.component.html" not in note


def test_fichier_hors_plan_signale():
    files = ["src/app/hello/hello.component.html", "src/app/footer/footer.component.ts"]
    assert out_of_scope(files, plan_hints(TASKS)) == ["src/app/footer/footer.component.ts"]


def test_hint_dossier_et_joker():
    files = ["src/app/hello/a.ts", "src/assets/i18n/fr.json", "README.md"]
    assert out_of_scope(files, ["src/app/hello/", "src/assets/i18n/*.json"]) == ["README.md"]


def test_tout_dans_le_plan_aucune_note():
    assert scope_note(["src/app/hello/hello.component.html"], plan_hints(TASKS)) == ""


def test_sans_hint_seul_spec_md_signale():
    assert out_of_scope(["x.ts", "SPEC.md"], []) == ["SPEC.md"]


# ---------------------------------------------------- commit_all (vrai git) ---
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "qa@test")
    _git(tmp_path, "config", "user.name", "QA")
    (tmp_path / "app.html").write_text("<div>50 000+</div>\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _committed(repo):
    return _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_commit_all_n_ajoute_jamais_spec_md(repo):
    (repo / "app.html").write_text("<div>90 000+</div>\n")
    (repo / "SPEC.md").write_text("# Spec\n")
    asyncio.run(git_tools.commit_all(str(repo), "review 1: applique le retour"))
    assert "SPEC.md" not in _committed(repo)
    assert (repo / "SPEC.md").exists()  # reste local pour la gate
    assert "90 000+" in _git(repo, "show", "HEAD:app.html")


def test_commit_all_retire_un_spec_md_deja_committe(repo):
    (repo / "SPEC.md").write_text("# Spec\n")
    _git(repo, "add", "SPEC.md")
    _git(repo, "commit", "-qm", "erreur: SPEC.md committé")
    (repo / "app.html").write_text("<div>90 000+</div>\n")
    asyncio.run(git_tools.commit_all(str(repo), "review 2"))
    assert "SPEC.md" not in _committed(repo)
    assert (repo / "SPEC.md").exists()


def test_diff_contre_origin_quand_les_fusions_sont_sur_develop(tmp_path):
    """Constat TOOLLIVE-3604 / MR !1592 : les fusions se font sur le develop LOCAL,
    donc `develop...HEAD` était vide et le rapport QA disait « Aucun fichier modifié »."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "develop")
    _git(origin, "config", "user.email", "qa@test")
    _git(origin, "config", "user.name", "QA")
    (origin / "hello.component.html").write_text("<div>Partners with a ToolLive account</div>\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "init")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "qa@test")
    _git(work, "config", "user.name", "QA")
    (work / "hello.component.html").write_text("<div>Partners using ToolLive</div>\n")
    _git(work, "commit", "-qam", "merge agent/t1")  # fusion sur le develop local
    diff = asyncio.run(git_tools.diff_against_base(str(work)))
    assert modified_files(diff["diff"]) == ["hello.component.html"]
    assert diff["base"] == "develop"


def test_branche_release_deja_sur_le_remote(tmp_path):
    """Constat TOOLLIVE-3607 (relance) : push refusé car release/<change_id>
    existait déjà sur GitLab ; remote_branch_exists permet de choisir un autre nom."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "qa@test")
    _git(work, "config", "user.name", "QA")
    (work / "a.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/release/fix-typo")
    assert asyncio.run(git_tools.remote_branch_exists(str(work), "release/fix-typo"))["exists"]
    assert not asyncio.run(git_tools.remote_branch_exists(str(work), "release/autre"))["exists"]
