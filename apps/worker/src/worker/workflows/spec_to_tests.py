"""Orchestrateur durable : spec (interview développeur + SPEC.md) → gate
humaine → plan → dev parallèle (worktrees) → boucle tests bornée → fusions
séquentielles → gate humaine finale → MR ouverte (merge manuel sur le forge).

Entrées :
    repo     : URL du repo cible (https://gitlab.com/<org>/<repo>.git — cloné
               dans `pipeline_workspace`) OU chemin local (mode dev)
    request  : demande produit de l'humain, en langage naturel. Peut contenir
               « MR <iid> » (ex. "repars de la MR 1585 : ...") pour compléter une
               MR en cours : sa branche source devient la base de travail et le
               push final la met à jour (la MR n'est pas recréée ni supprimée).
    jira_issue : clé d'un ticket JIRA (ex. "TOOLLIVE-3600"), lu via le connecteur
               MCP `atlassian` (@uses_connectors + activities/jira_tools.py) : le
               contenu du ticket alimente la demande, le ticket reçoit les
               commentaires de progression et les transitions de statut.

Gates humaines : le workflow enregistre deux signaux `approve` / `reject`
(scripts/approve.py <execution_id> approve|reject) et dort sur
workflow.wait_condition — aucun coût compute pendant l'attente.
"""

import asyncio
import json
import re
from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows import workflow as wf
from mistralai.workflows.plugins.mistralai.connectors import uses_connectors

from worker.agents.definitions import (
    MAX_DEV_ITERATIONS,
    MAX_EMPTY_SPEC_ROUNDS,
    MAX_PLANNER_ATTEMPTS,
    MAX_REVIEW_ROUNDS,
)

with wf.unsafe.imports_passed_through():
    from worker.activities import agent_runner, git_tools, jira_tools, test_runner
    from worker.activities.jira_tools import atlassian_connector
    from worker.activities.qa_report import format_qa_report

GATE_TIMEOUT = timedelta(hours=24)

# Timeout d'exécution déclaré sur le workflow : le SDK mistralai-workflows met
# 1h par défaut (@workflow.define(execution_timeout=...)) — c'est ce plafond
# qui tuait l'exécution en TIMED_OUT à 3600004 ms. On le monte à 6h.
WORKFLOW_EXECUTION_TIMEOUT = timedelta(hours=6)


def _prefixed_line(text: str, prefix: str) -> str | None:
    """Extrait le contenu de la première ligne commençant par `prefix`."""
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):].strip()
    return None


def _extract_json(text: str):
    """Extrait le JSON d'une réponse d'agent : le modèle peut l'encadrer de
    prose ou de balises markdown ```json ... ```, voire renvoyer une chaîne
    vide si la boucle atteint MAX_AGENT_TURNS."""
    text = (text or "").strip()
    if not text:
        raise RuntimeError("réponse de l'agent vide (MAX_AGENT_TURNS atteint ?)")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise RuntimeError(f"aucun JSON dans la réponse de l'agent: {text[:300]!r}")


@workflows.workflow.define(
    name="spec-to-tests",
    workflow_display_name="Spec → Dev → Tests",
    execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
    workflow_description="Pipeline multi-agents: agent spec (interview développeur + SPEC.md, "
                          "interactif: signaux approve/reject/reply + query state), "
                          "dev parallèle en worktrees, boucle tests bornée, "
                          "fusions, Merge Request.",
)
@uses_connectors(atlassian_connector)
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

    async def _wait_review_verdict(self, consumed: int) -> tuple[str, str]:
        """Gate review : dort jusqu'à `approve`/`reject` (verdict) OU un nouveau
        message `reply` (retour de review). Retourne (verdict, feedback)."""
        try:
            await wf.wait_condition(
                lambda: self.verdict is not None or len(self.conversation) > consumed,
                timeout=GATE_TIMEOUT)
        except asyncio.TimeoutError:
            return "timeout", ""
        if self.verdict is not None:
            verdict, self.verdict = self.verdict, None
            return verdict or "timeout", ""
        feedback = "\n".join(m["text"] for m in self.conversation[consumed:]
                             if m.get("role") == "human")
        return "", feedback

    async def _gate(self, subject: str, payload: str, execution_id: str,
                    jira_key: str) -> None:
        """Notification humaine (webhook open_approval) + commentaire JIRA sur le
        ticket lié s'il y en a un."""
        await test_runner.open_approval(subject=subject, payload=payload,
                                        execution_id=execution_id)
        if jira_key:
            await jira_tools.jira_comment(jira_key, f"🔔 *{subject}*\n\n{payload[:2000]}")

    async def _jira_note(self, jira_key: str, text: str) -> None:
        """Commentaire JIRA de progression (no-op sans ticket lié)."""
        if jira_key:
            await jira_tools.jira_comment(jira_key, text)

    @workflows.workflow.entrypoint
    async def run(self, repo: str, request: str, jira_issue: str = "") -> dict:
        # ------------------------------------------------------------------
        # 0. WORKSPACE — URL (GitLab/GitHub) -> clone local ; sinon chemin local
        ws = await git_tools.ensure_workspace(repo)
        repo, repo_url = ws["path"], ws["url"]
        execution_id = workflows.get_execution_id()

        # ------------------------------------------------------------------
        # 0b. TICKET JIRA (optionnel) — lu via le connecteur MCP `atlassian` :
        #     le contenu du ticket (description + commentaires) devient le
        #     contexte de la demande, le ticket passe en transition de démarrage
        #     et un commentaire annonce l'execution_id + les commandes humaines.
        jira_key = (jira_issue or "").strip()
        if jira_key:
            issue = await jira_tools.fetch_jira_issue(jira_key)
            if issue.get("ok") is False:
                # Connecteur indisponible sous l'identité du run (ex. déploiement
                # managé sans credential workspace sur un connecteur OAuth user-only,
                # 401 « Authentication failed against MCP server ») : le pipeline
                # continue sans le contenu du ticket ; commentaires et transitions
                # JIRA sont de toute façon non bloquants.
                request = (f"[Ticket JIRA {jira_key} illisible sous l'identité du run : "
                           f"{issue.get('error') or issue.get('reason')}]\n\n"
                           f"Demande initiale :\n{request}")
            else:
                comments_txt = "\n".join(
                    f"- {c['author'] or '?'}: {c['body'][:500]}" for c in issue["comments"][-10:])
                ticket_block = (
                    f"[Ticket JIRA {issue['key']} — {issue['summary']}] {issue['url']}\n"
                    f"Statut: {issue['status']} | Type: {issue['issuetype']} | "
                    f"Priorité: {issue['priority']} | Labels: {', '.join(issue['labels']) or '-'} | "
                    f"Assigné: {issue['assignee'] or '-'}\n\n"
                    f"Description du ticket :\n{issue['description'] or '(vide)'}"
                    + (f"\n\nCommentaires existants :\n{comments_txt}" if comments_txt else ""))
                request = f"{ticket_block}\n\nDemande initiale :\n{request}"
                start = await jira_tools.jira_transition_start(jira_key)
                start_note = (f"Statut passé à « {start.get('to') or start.get('transition')} »."
                              if start.get("ok")
                              else f"Transition de démarrage non appliquée "
                                   f"({start.get('reason') or start.get('error', 'inconnue')}).")
                await jira_tools.jira_comment(
                    jira_key,
                    f"🤖 Pipeline `spec-to-tests` démarré sur {issue['key']} — {issue['summary']}"
                    f" (exécution `{execution_id}`, repo `{repo_url or repo}`)\n{start_note}\n"
                    f"Suivi : `python scripts/status.py {execution_id}` — gates : "
                    f"`python scripts/approve.py {execution_id} approve|reject` — réponses à "
                    f"l'agent spec : `python scripts/talk.py {execution_id} \"...\"`",
                )

        # Mode « reprendre une MR en cours » : la demande contient « MR <iid> »
        # (ex. "repars de la MR 1585 : ...") -> on fetch la branche source de la MR
        # et on travaille dessus ; le push final met à jour CETTE MR (pas de
        # nouvelle branche release ni de nouvelle MR).
        mr_info = None
        if repo_url:
            m = re.search(r"\bMR\s*#?\s*(\d+)\b", request, re.IGNORECASE)
            if m:
                mr_info = await git_tools.get_merge_request(repo_url, int(m.group(1)))
                await git_tools.checkout_remote_branch(repo, mr_info["source_branch"])

        # ------------------------------------------------------------------
        # 1. AGENT SPEC — interview interactif SANS LIMITE DE TOURS :
        #    l'agent pose des questions (ligne "QUESTION: ...") pour valider le
        #    changement avec le développeur, le workflow dort sur le signal
        #    `reply` (scripts/talk.py) et relance l'agent avec la réponse.
        #    L'agent écrit SPEC.md puis termine par "CHANGE_ID: <id>".
        #    Le transcript est interrogeable à tout moment (query `state`).
        #    Seule garde : MAX_EMPTY_SPEC_ROUNDS tours consécutifs sans aucune
        #    progression (ni QUESTION ni CHANGE_ID) -> escalade anti boucle vide.
        self.phase = "spec"
        self.conversation.append({"role": "human", "text": request})
        if mr_info:
            self.conversation.append({"role": "human", "text": (
                f"[Contexte : reprise de la MR !{mr_info['iid']} ({mr_info['web_url']}) — "
                f"la branche source `{mr_info['source_branch']}` est la base de travail ; "
                f"le push final mettra à jour CETTE MR (ne pas en ouvrir une nouvelle).]")})
        change_id = ""
        round_no = 0
        empty_rounds = 0
        while not change_id:
            round_no += 1
            out = await agent_runner.run_agent_activity(
                "spec", self._transcript(), "spec", repo)
            question = _prefixed_line(out, "QUESTION:")
            cid = _prefixed_line(out, "CHANGE_ID:")
            if not question and not cid:
                empty_rounds += 1
                if empty_rounds >= MAX_EMPTY_SPEC_ROUNDS:
                    await self._gate(
                        subject="Interview spec sans progression",
                        payload=(f"L'agent spec n'a ni posé de question ni renvoyé de CHANGE_ID "
                                 f"depuis {MAX_EMPTY_SPEC_ROUNDS} tours (réponses vides) — "
                                 f"le workflow va échouer (escalade).\n"
                                 f"Transcript : python scripts/status.py {execution_id}\n"
                                 f"Arrêter proprement : python scripts/approve.py {execution_id} reject"),
                        execution_id=execution_id, jira_key=jira_key,
                    )
                    raise RuntimeError(
                        f"interview spec sans progression depuis {MAX_EMPTY_SPEC_ROUNDS} "
                        f"tours (réponses vides) — escalade humaine (query `state`, signal `reject`)")
                continue
            empty_rounds = 0
            if question and not cid:
                self.pending_question = question
                self.conversation.append({"role": "agent", "text": question})
                await self._gate(
                    subject=f"Question de l'agent spec (tour {round_no})",
                    payload=(f"{question}\n\nRéponds à l'agent : python scripts/talk.py "
                             f"{execution_id} \"ta réponse\"\nForcer la finalisation : "
                             f"python scripts/approve.py {execution_id} approve\n"
                             f"Voir le transcript : python scripts/status.py {execution_id}"),
                    execution_id=execution_id, jira_key=jira_key,
                )
                consumed = self.replies
                try:
                    await wf.wait_condition(
                        lambda: self.replies > consumed or self.verdict is not None,
                        timeout=GATE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._gate(
                        subject=f"Interview spec sans réponse depuis 24h (tour {round_no})",
                        payload=(f"Aucune réponse reçue sur la question en attente.\n"
                                 f"Transcript : python scripts/status.py {execution_id}\n"
                                 f"Répondre à l'agent : python scripts/talk.py {execution_id} \"...\"\n"
                                 f"Arrêter : python scripts/approve.py {execution_id} reject"),
                        execution_id=execution_id, jira_key=jira_key,
                    )
                    await self._jira_note(
                        jira_key,
                        f"⏳ Interview spec sans réponse depuis 24h — exécution `{execution_id}` "
                        f"en pause (transcript : `python scripts/status.py {execution_id}`).")
                    return {"status": "timeout_spec_interview", "jira_issue": jira_key or None,
                            "conversation": self.conversation}
                if self.verdict == "reject":
                    await self._jira_note(jira_key, "❌ Run rejeté pendant l'interview spec.")
                    return {"status": "rejected_at_spec", "change_id": None,
                            "jira_issue": jira_key or None}
                if self.verdict == "approve":
                    # l'humain veut finaliser sans attendre la fin de l'interview
                    self.verdict = None
                    self.conversation.append({
                        "role": "human",
                        "text": "[Finalise la spec maintenant, ne pose plus de question.]",
                    })
                continue
            change_id = cid or ""
        self.pending_question = None
        self.conversation.append({"role": "agent", "text": f"CHANGE_ID: {change_id}"})

        # ------------------------------------------------------------------
        # 2. GATE HUMAINE — l'humain relit proposal.md + specs/
        self.phase = "gate_spec"
        await self._gate(
            subject=f"Spec à valider: {change_id}",
            payload=f"Spec à valider : {repo}/SPEC.md — réponds "
                    f"`approve` ou `reject` (python scripts/approve.py {execution_id} "
                    f"approve|reject) ; transcript : python scripts/status.py {execution_id}",
            execution_id=execution_id, jira_key=jira_key,
        )
        if await self._wait_verdict() != "approve":
            await self._jira_note(
                jira_key,
                f"❌ SPEC.md rejetée à la gate de validation (change_id `{change_id}`).")
            return {"status": "rejected_at_spec", "change_id": change_id,
                    "jira_issue": jira_key or None}

        # ------------------------------------------------------------------
        # 3. AGENT PLANNER — tasks.md -> tâches unitaires avec dépendances
        self.phase = "plan"
        plan_prompt = (f"Lis SPEC.md à la racine du repo (change_id: {change_id}) — retourne UNIQUEMENT "
                       f"le JSON {{\"tasks\": [{{id, title, capability, files_hint, depends_on, acceptance}}]}}")
        plan = ""
        tasks = None
        for attempt in range(MAX_PLANNER_ATTEMPTS):
            plan = await agent_runner.run_agent_activity(
                "planner",
                plan_prompt if attempt == 0 else (
                    plan_prompt + "\n\nTa réponse précédente était vide ou sans JSON valide. "
                    "Réponds MAINTENANT, sans appeler aucun outil, avec UNIQUEMENT l'objet JSON "
                    "{\"tasks\": [...]} — aucune prose, aucun markdown."),
                "spec",
                repo,
            )
            try:
                plan_obj = _extract_json(plan)
            except RuntimeError:
                continue
            tasks = plan_obj.get("tasks") if isinstance(plan_obj, dict) else plan_obj
            if tasks:
                break
        if not tasks:
            raise RuntimeError(
                f"planner: aucune tâche après {MAX_PLANNER_ATTEMPTS} tentatives (dernière réponse: {plan[:300]!r})")
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
            if not gate["passed"] and not gate.get("unrunnable"):
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
        if mr_info:
            # MR en cours : on push les fusions sur SA branche source (fast-forward)
            await git_tools.push_branch(repo, mr_info["source_branch"])
            mr = {"mr": {"iid": mr_info["iid"], "web_url": mr_info["web_url"]}}
        elif repo_url:
            await git_tools.create_branch(repo, release_branch)
            await git_tools.push_branch(repo, release_branch)
            mr_description = diff["diff"][:4000]
            if final.get("unrunnable"):
                mr_description += ("\n\n---\nTests non exécutables dans le worker (OOM killer, "
                                   "RAM fixée par la plateforme) : gate sautée, à valider via "
                                   "la CI de cette MR.")
            mr = await git_tools.open_merge_request(
                repo_url, release_branch, base,
                title=f"[{change_id}] pipeline spec-to-tests",
                description=mr_description,
            )
        push_target = ((await git_tools.current_branch(repo))["branch"] if repo_url
                       else release_branch)
        review_link = (mr["mr"] or {}).get("web_url") or f"{repo} (diff vs {base})"
        if jira_key:
            rev = await jira_tools.jira_transition_review(jira_key)
            rev_note = (f" Statut passé à « {rev.get('to') or rev.get('transition')} »."
                        if rev.get("ok") else "")
            # Lire SPEC.md pour le rapport QA
            spec_result = await git_tools.read_spec(repo)
            spec_content = spec_result.get("content", "") if spec_result.get("ok") else ""
            # Formater le rapport QA
            qa_report = format_qa_report(
                spec_content=spec_content,
                review_link=review_link,
                diff_output=diff.get("diff", ""),
                test_results=final,
            )
            # Ajouter la note de statut si présente
            if rev_note:
                qa_report += f"\n\n{rev_note}"
            await jira_tools.jira_comment(jira_key, qa_report)

        # ------------------------------------------------------------------
        # 6. GATE REVIEW — un signal `reply` = retour de review : le pipeline
        #    corrige la branche, re-push la MR et redemande (MAX_REVIEW_ROUNDS max).
        for review_round in range(MAX_REVIEW_ROUNDS):
            await self._gate(
                subject=(f"Review finale: {change_id} — MR: {review_link}"
                         + (f" (tour {review_round + 1})" if review_round else "")),
                payload=(f"{review_link}\n\n{diff['diff'][:2000]}\n\nTests:\n{final['log'][:1500]}"
                         f"\n\n`approve` = validé, `reject` = rejeté, tout autre message (`reply`) "
                         f"= retour de review traité par le pipeline (correction + re-push)."),
                execution_id=execution_id, jira_key=jira_key,
            )
            consumed = len(self.conversation)
            verdict, feedback = await self._wait_review_verdict(consumed)
            if verdict == "approve":
                self.phase = "done"
                if jira_key:
                    done = await jira_tools.jira_transition_done(jira_key)
                    done_note = (f" Statut passé à « {done.get('to') or done.get('transition')} »."
                                 if done.get("ok") else "")
                    await jira_tools.jira_comment(
                        jira_key,
                        f"✅ Review approuvée — MR : {(mr.get('mr') or {}).get('web_url')}"
                        f"{done_note} (merge manuel sur le forge).")
                return {"status": "mr_opened", "change_id": change_id,
                        "jira_issue": jira_key or None,
                        "mr": (mr.get("mr") or {}).get("web_url"), "tests": final}
            if verdict == "reject" or not feedback:
                await self._jira_note(
                    jira_key,
                    f"❌ Review rejetée — MR : {(mr['mr'] or {}).get('web_url')} "
                    f"(change_id `{change_id}`).")
                return {"status": "rejected_at_review", "change_id": change_id,
                        "jira_issue": jira_key or None,
                        "mr": (mr["mr"] or {}).get("web_url")}
            # retour de review (`reply`) -> correctif sur la branche MR, puis re-push
            self.phase = "review_fix"
            fix_context = (f"Retour de review sur {review_link} (branche courante « {push_target} » "
                           f"):\n{feedback}\nCorrige le code en conséquence sans rien casser d'autre.")
            await agent_runner.run_agent_activity("dev", fix_context, "dev", repo)
            await git_tools.commit_all(repo, f"review {review_round + 1}: applique le retour de review")
            final = await test_runner.run_tests(repo)
            if repo_url:
                await git_tools.push_branch(repo, push_target)
            diff = await git_tools.diff_against_base(repo)

        # ------------------------------------------------------------------
        # 7. ESCALADE — pas de merge automatique de la MR (l'auto-merge échouait en
        # HTTP 405 côté forge) : la MR reste ouverte, merge manuel après review.
        self.phase = "done"
        await self._jira_note(
            jira_key,
            f"⚠️ {MAX_REVIEW_ROUNDS} tours de review atteints sans approbation — MR : "
            f"{(mr.get('mr') or {}).get('web_url')} (escalade humaine).")
        return {"status": "mr_updated" if mr_info else "mr_opened", "change_id": change_id,
                "jira_issue": jira_key or None,
                "mr": (mr.get("mr") or {}).get("web_url"), "tests": final}

    # ---------------------------------------------------------------- boucle
    async def _run_dev_task(self, repo: str, change_id: str, task: dict) -> dict:
        """Boucle bornée dev -> tests (escalade humaine au bout de N tours)."""
        wt = await git_tools.create_worktree(repo, task["id"])

        context = (f"Spec validée : SPEC.md à la racine du repo (change_id: {change_id}). "
                   f"Ta tâche: {json.dumps(task)}")

        for iteration in range(MAX_DEV_ITERATIONS):
            await agent_runner.run_agent_activity("dev", context, "dev", wt["worktree"])
            await git_tools.commit_all(wt["worktree"], f"task {task['id']}: {task['title']}")
            await agent_runner.run_agent_activity("tester", context, "dev", wt["worktree"])
            result = await test_runner.run_tests(wt["worktree"])
            if result["passed"]:
                return wt
            if result.get("missing_worktree"):
                raise RuntimeError(
                    f"task {task['id']}: worktree {wt['worktree']} introuvable — gate de "
                    f"tests inexécutable (worktree supprimé pendant la boucle dev ou worker "
                    f"redémarré) ; escalade humaine")
            if result.get("unrunnable"):
                return wt
            context += f"\n\nECHEC TESTS (tour {iteration + 1}):\n{result['log'][:3000]}"

        raise RuntimeError(
            f"task {task['id']}: boucle non convergée après "
            f"{MAX_DEV_ITERATIONS} itérations — escalade humaine\n"
            f"dernier log de tests:\n{result['log'][:2000]}")
