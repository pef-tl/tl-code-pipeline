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

from worker.agents.definitions import MAX_DEV_ITERATIONS, MAX_SPEC_ROUNDS

with wf.unsafe.imports_passed_through():
    from worker.activities import agent_runner, git_tools, test_runner
    from worker.activities.openspec_tools import archive_change

GATE_TIMEOUT = timedelta(hours=24)


def _prefixed_line(text: str, prefix: str) -> str | None:
    """Extrait le contenu de la première ligne commençant par `prefix`."""
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):].strip()
    return None


@workflows.workflow.define(
    name="spec-to-tests",
    workflow_display_name="Spec → Dev → Tests",
    workflow_description="Pipeline multi-agents: agent spec OpenSpec (interview "
                          "interactif: signaux approve/reject/reply + query state), "
                          "dev parallèle en worktrees, boucle tests bornée, "
                          "fusions, Merge Request.",
)
class SpecToTests:

    def __init__(self) -> None:
        self.verdict: str | None = None
        self.phase: str = "init"
        self.pending_question: str | None = None
        self.conversation: list[dict] = []
        self.replies: int = 0

    @workflows.workflow.signal(name="approve")
    async def on_approve(self) -> None:
        self.verdict = "approve"

    @workflows.workflow.signal(name="reject")
    async def on_reject(self) -> None:
        self.verdict = "reject"

    @workflows.workflow.signal(name="reply")
    async def on_reply(self, text: str) -> None:
        """Message libre de l'humain, transmis à l'agent en cours (interview
        spec...). Envoyé par `python scripts/talk.py <execution_id> "..."`."""
        self.conversation.append({"role": "human", "text": text})
        self.replies += 1

    @workflows.workflow.query(name="state")
    def get_state(self) -> dict:
        """Lecture seule de l'état courant — interrogeable à tout moment :
        `python scripts/status.py <execution_id>`."""
        return {
            "phase": self.phase,
            "pending_question": self.pending_question,
            "verdict": self.verdict,
            "conversation": [dict(m) for m in self.conversation],
        }

    def _transcript(self) -> str:
        return "\n".join(f"{m['role']}: {m['text']}" for m in self.conversation)

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
        # 1. AGENT SPEC — interview interactif borné (MAX_SPEC_ROUNDS) :
        #    l'agent pose des questions (ligne "QUESTION: ..."), le workflow
        #    dort sur le signal `reply` (scripts/talk.py) et relance l'agent
        #    avec la réponse. Verdict final : ligne "CHANGE_ID: <id>".
        #    Le transcript est interrogeable à tout moment (query `state`).
        self.phase = "spec"
        self.conversation.append({"role": "human", "text": request})
        change_id = ""
        round_no = 0
        while not change_id:
            if round_no >= MAX_SPEC_ROUNDS:
                raise RuntimeError(
                    f"interview spec non convergé après {MAX_SPEC_ROUNDS} "
                    f"tours — escalade humaine (query `state`, signal `reject`)")
            out = await agent_runner.run_agent_activity(
                "spec", self._transcript(), "spec", repo)
            question = _prefixed_line(out, "QUESTION:")
            cid = _prefixed_line(out, "CHANGE_ID:")
            if question and not cid:
                self.pending_question = question
                self.conversation.append({"role": "agent", "text": question})
                await test_runner.open_approval(
                    subject=f"Question de l'agent spec (tour {round_no + 1})",
                    payload=(f"{question}\n\nRéponds à l'agent : python scripts/talk.py "
                             f"{execution_id} \"ta réponse\"\nForcer la finalisation : "
                             f"python scripts/approve.py {execution_id} approve\n"
                             f"Voir le transcript : python scripts/status.py {execution_id}"),
                    execution_id=execution_id,
                )
                consumed = self.replies
                try:
                    await wf.wait_condition(
                        lambda: self.replies > consumed or self.verdict is not None,
                        timeout=GATE_TIMEOUT)
                except asyncio.TimeoutError:
                    return {"status": "timeout_spec_interview",
                            "conversation": self.conversation}
                if self.verdict == "reject":
                    return {"status": "rejected_at_spec", "change_id": None}
                if self.verdict == "approve":
                    # l'humain veut finaliser sans attendre la fin de l'interview
                    self.verdict = None
                    self.conversation.append({
                        "role": "human",
                        "text": "[Finalise la spec maintenant, ne pose plus de question.]",
                    })
                round_no += 1
                continue
            change_id = (cid or out.strip()).strip()
        self.pending_question = None
        self.conversation.append({"role": "agent", "text": f"CHANGE_ID: {change_id}"})

        # ------------------------------------------------------------------
        # 2. GATE HUMAINE — l'humain relit proposal.md + specs/
        self.phase = "gate_spec"
        await test_runner.open_approval(
            subject=f"Spec à valider: {change_id}",
            payload=f"{repo}/openspec/changes/{change_id}/proposal.md — réponds "
                    f"`approve` ou `reject` (python scripts/approve.py {execution_id} "
                    f"approve|reject) ; transcript : python scripts/status.py {execution_id}",
            execution_id=execution_id,
        )
        if await self._wait_verdict() != "approve":
            return {"status": "rejected_at_spec", "change_id": change_id}

        # ------------------------------------------------------------------
        # 3. AGENT PLANNER — tasks.md -> tâches unitaires avec dépendances
        self.phase = "plan"
        plan = await agent_runner.run_agent_activity(
            "planner",
            f"Change folder: openspec/changes/{change_id}/ — retourne UNIQUEMENT "
            f"le JSON {{\"tasks\": [{{id, title, capability, files_hint, depends_on, acceptance}}]}}",
            "spec",
            repo,
        )
        tasks = json.loads(plan)["tasks"]
        by_id = {t["id"]: t for t in tasks}

        # ------------------------------------------------------------------
        # 4. DEV PARALLÈLE — un worktree par tâche sans dépendance mutuelle
        self.phase = "dev"
        pending = set(by_id)
        done: set[str] = set()
        worktrees: dict[str, str] = {}
        while pending:
            ready = [by_id[i] for i in sorted(pending)
                     if all(d in done for d in by_id[i].get("depends_on", []))]
            if not ready:
                raise RuntimeError(f"deadlock on tasks: {pending}")

            results = await asyncio.gather(
                *(self._run_dev_task(repo, change_id, task) for task in ready)
            )
            for task, wt in zip(ready, results):
                done.add(task["id"])
                pending.discard(task["id"])
                worktrees[task["id"]] = wt["worktree"]

        # ------------------------------------------------------------------
        # 5. FUSIONS SÉQUENTIELLES — re-run complet de la suite après chacune
        self.phase = "merge"
        for task in tasks:
            await git_tools.merge_branch(repo, f"agent/{task['id']}")
            gate = await test_runner.run_tests(repo)
            if not gate["passed"]:
                raise RuntimeError(
                    f"regression après fusion {task['id']}: {gate['log'][:500]}")
            await git_tools.cleanup_worktree(repo, worktrees[task["id"]], f"agent/{task['id']}")

        # ------------------------------------------------------------------
        # 6. GATE HUMAINE FINALE — branche release + MR GitLab + review du diff
        self.phase = "gate_review"
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
        self.phase = "done"
        await archive_change(repo, change_id)
        return {"status": "merged", "change_id": change_id,
                "mr": (mr.get("mr") or {}).get("web_url"), "tests": final}

    # ---------------------------------------------------------------- boucle
    async def _run_dev_task(self, repo: str, change_id: str, task: dict) -> dict:
        """Boucle bornée dev -> tests (escalade humaine au bout de N tours)."""
        wt = await git_tools.create_worktree(repo, task["id"])

        context = (f"Change: openspec/changes/{change_id}/ (lis tasks.md, design.md, "
                   f"specs/). Ta tâche: {json.dumps(task)}")

        for iteration in range(MAX_DEV_ITERATIONS):
            await agent_runner.run_agent_activity("dev", context, "dev", wt["worktree"])
            await git_tools.commit_all(wt["worktree"], f"task {task['id']}: {task['title']}")
            await agent_runner.run_agent_activity("tester", context, "dev", wt["worktree"])
            result = await test_runner.run_tests(wt["worktree"])
            if result["passed"]:
                return wt
            context += f"\n\nECHEC TESTS (tour {iteration + 1}):\n{result['log'][:3000]}"

        raise RuntimeError(
            f"task {task['id']}: boucle non convergée après "
            f"{MAX_DEV_ITERATIONS} itérations — escalade humaine")
