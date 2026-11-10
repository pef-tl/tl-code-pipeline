"""Orchestrateur durable : spec (OpenSpec) → gate humaine → plan → dev
parallèle (worktrees) → boucle tests bornée → fusions séquentielles →
gate humaine finale (Merge Request) → merge → archive OpenSpec.

Entrées :
    repo     : URL du repo cible (https://gitlab.com/<org>/<repo>.git — cloné
               dans `pipeline_workspace`) OU chemin local (mode dev)
    request  : demande produit de l'humain, en langage naturel

Gates humaines : le workflow enregistre deux signaux `approve` / `reject`
(scripts/approve.py <execution_id> approve|reject) et dort sur
workflow.wait_condition — aucun coût compute pendant l'attente.
"""

import asyncio
import json
from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows import workflow as wf

from worker.activities import git_tools, test_runner
from worker.activities.openspec_tools import archive_change, build_spec_tools
from worker.agents import definitions as defs
from worker.agents.run_agent import run_agent
from worker.agents.tools import build_dev_tools

GATE_TIMEOUT = timedelta(hours=24)


@workflows.workflow.define(
    name="spec-to-tests",
    workflow_display_name="Spec → Dev → Tests",
    workflow_description="Pipeline multi-agents: agent spec OpenSpec, gates humaines "
                          "(signaux approve/reject), dev parallèle en worktrees, "
                          "boucle tests bornée, fusions, Merge Request.",
)
class SpecToTests:

    def __init__(self) -> None:
        self.verdict: str | None = None

    @workflows.workflow.signal(name="approve")
    async def on_approve(self) -> None:
        self.verdict = "approve"

    @workflows.workflow.signal(name="reject")
    async def on_reject(self) -> None:
        self.verdict = "reject"

    async def _wait_verdict(self) -> str:
        """Dort jusqu'au prochain signal approve/reject (ou timeout 24h)."""
        try:
            await wf.wait_condition(lambda: self.verdict is not None, timeout=GATE_TIMEOUT)
        except asyncio.TimeoutError:
            return "timeout"
        verdict, self.verdict = self.verdict, None
        return verdict or "timeout"

    @workflows.workflow.entrypoint
    async def run(self, repo: str, request: str) -> dict:
        # ------------------------------------------------------------------
        # 0. WORKSPACE — URL (GitLab/GitHub) -> clone local ; sinon chemin local
        ws = await git_tools.ensure_workspace(repo)
        repo, repo_url = ws["path"], ws["url"]
        execution_id = workflows.get_execution_id()

        # ------------------------------------------------------------------
        # 1. AGENT SPEC — skill openspec-propose : produit le change folder
        #    openspec/changes/<id>/{proposal.md, design.md, tasks.md, specs/}
        spec = defs.spec_agent(tools=build_spec_tools(repo))
        change_id = await run_agent(spec, request, build_spec_tools(repo))
        change_id = change_id.strip()

        # ------------------------------------------------------------------
        # 2. GATE HUMAINE — l'humain relit proposal.md + specs/
        await test_runner.open_approval(
            subject=f"Spec à valider: {change_id}",
            payload=f"{repo}/openspec/changes/{change_id}/proposal.md — réponds "
                    f"`approve` ou `reject` sur l'exécution {execution_id}",
            execution_id=execution_id,
        )
        if await self._wait_verdict() != "approve":
            return {"status": "rejected_at_spec", "change_id": change_id}

        # ------------------------------------------------------------------
        # 3. AGENT PLANNER — tasks.md -> tâches unitaires avec dépendances
        planner = defs.planner_agent(tools=[build_spec_tools(repo)["openspec_read"]])
        plan = await run_agent(
            planner,
            f"Change folder: openspec/changes/{change_id}/ — retourne UNIQUEMENT "
            f"le JSON {{\"tasks\": [{{id, title, capability, files_hint, depends_on, acceptance}}]}}",
            build_spec_tools(repo),
        )
        tasks = json.loads(plan)["tasks"]
        by_id = {t["id"]: t for t in tasks}

        # ------------------------------------------------------------------
        # 4. DEV PARALLÈLE — un worktree par tâche sans dépendance mutuelle
        pending = set(by_id)
        done: set[str] = set()
        worktrees: dict[str, str] = {}
        while pending:
            ready = [by_id[i] for i in sorted(pending)
                     if all(d in done for d in by_id[i].get("depends_on", []))]
            if not ready:
                raise RuntimeError(f"deadlock on tasks: {pending}")

            results = await workflows.workflow.parallel([
                self._run_dev_task.bind(repo, change_id, task) for task in ready
            ])
            for task, wt in zip(ready, results):
                done.add(task["id"])
                pending.discard(task["id"])
                worktrees[task["id"]] = wt["worktree"]

        # ------------------------------------------------------------------
        # 5. FUSIONS SÉQUENTIELLES — re-run complet de la suite après chacune
        for task in tasks:
            await git_tools.merge_branch(repo, f"agent/{task['id']}")
            gate = await test_runner.run_tests(repo)
            if not gate["passed"]:
                raise RuntimeError(
                    f"regression après fusion {task['id']}: {gate['log'][:500]}")
            await git_tools.cleanup_worktree(repo, worktrees[task["id"]], f"agent/{task['id']}")

        # ------------------------------------------------------------------
        # 6. GATE HUMAINE FINALE — branche release + MR GitLab + review du diff
        final = await test_runner.run_tests(repo)
        diff = await git_tools.diff_against_base(repo)
        base = diff["base"]
        release_branch = f"release/{change_id}"
        mr = {"mr": None}
        if repo_url:
            await git_tools.create_branch(repo, release_branch)
            await git_tools.push_branch(repo, release_branch)
            mr = await git_tools.open_merge_request(
                repo_url, release_branch, base,
                title=f"[{change_id}] pipeline spec-to-tests",
                description=diff["diff"][:4000],
            )
        review_link = (mr["mr"] or {}).get("web_url") or f"{repo} (diff vs {base})"
        await test_runner.open_approval(
            subject=f"Review finale: {change_id} — MR: {review_link}",
            payload=f"{review_link}\n\n{diff['diff'][:2000]}\n\nTests:\n{final['log'][:1500]}",
            execution_id=execution_id,
        )
        if await self._wait_verdict() != "approve":
            return {"status": "rejected_at_review", "change_id": change_id,
                    "mr": (mr["mr"] or {}).get("web_url")}

        # ------------------------------------------------------------------
        # 7. MERGE — MR GitLab (mode URL) ou push de la branche de base
        if repo_url and mr.get("mr"):
            merged = await git_tools.merge_merge_request(repo_url, mr["mr"]["iid"])
            if "error" in merged:
                raise RuntimeError(f"merge MR failed: {merged['error']}")
        elif repo_url:
            await git_tools.push_branch(repo, base)

        # 8. ARCHIVE OpenSpec — specs/ à jour, change archivé
        await archive_change(repo, change_id)
        return {"status": "merged", "change_id": change_id,
                "mr": (mr.get("mr") or {}).get("web_url"), "tests": final}

    # ---------------------------------------------------------------- boucle
    async def _run_dev_task(self, repo: str, change_id: str, task: dict) -> dict:
        """Boucle bornée dev -> tests (escalade humaine au bout de N tours)."""
        wt = await git_tools.create_worktree(repo, task["id"])
        dev = defs.dev_agent(tools=build_dev_tools(wt["worktree"]))
        tester = defs.tester_agent(tools=build_dev_tools(wt["worktree"]))

        context = (f"Change: openspec/changes/{change_id}/ (lis tasks.md, design.md, "
                   f"specs/). Ta tâche: {json.dumps(task)}")

        for iteration in range(defs.MAX_DEV_ITERATIONS):
            await run_agent(dev, context, build_dev_tools(wt["worktree"]))
            await git_tools.commit_all(wt["worktree"], f"task {task['id']}: {task['title']}")
            await run_agent(tester, context, build_dev_tools(wt["worktree"]))
            result = await test_runner.run_tests(wt["worktree"])
            if result["passed"]:
                return wt
            context += f"\n\nECHEC TESTS (tour {iteration + 1}):\n{result['log'][:3000]}"

        raise RuntimeError(
            f"task {task['id']}: boucle non convergée après "
            f"{defs.MAX_DEV_ITERATIONS} itérations — escalade humaine")
