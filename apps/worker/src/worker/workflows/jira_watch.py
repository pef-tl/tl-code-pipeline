"""Scrutateur JIRA : lance le pipeline `spec-to-tests` sur les tickets confiés
aux agents (statut « A faire » + étiquette `agent`, JQL configurable).

Une exécution = un passage court. La régularité vient d'une planification
Studio (`python scripts/schedule_watch.py`, ex. toutes les 10 min en heures
ouvrées), pas d'une boucle infinie : rien ne tourne entre deux passages.

Pour chaque ticket trouvé (au plus `max_starts` par passage, la RAM du worker
étant fixe) :
1. le scrutateur PREND le ticket : « A faire » -> « Pris en charge », sous la
   garde du workflow v4 (by_human=False : c'est l'agent qui le prend). Le
   ticket sort donc de la JQL : jamais deux pipelines sur le même ticket, même
   si deux passages se chevauchent. Refus ou échec -> ticket ignoré ;
2. il démarre `spec-to-tests` en exécution enfant détachée (ABANDON : le
   pipeline continue après la fin du passage) ;
3. si le démarrage échoue, le ticket passe en « blocked » avec le motif.

Ensuite tout se passe comme un lancement manuel : questions de l'agent spec et
gates dans le commentaire « Suivi » du ticket, réponses via scripts/talk.py /
scripts/approve.py ou Studio.
"""

from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows import workflow as wf
from mistralai.workflows.plugins.mistralai.connectors import uses_connectors
from pydantic import BaseModel

with wf.unsafe.imports_passed_through():
    from worker.activities import jira_tools, jira_watch
    from worker.activities.jira_tools import atlassian_connector

from worker.workflows.spec_to_tests import SpecToTests

MAX_STARTS_DEFAULT = 2


class SpecToTestsInput(BaseModel):
    """Entrée de `spec-to-tests` (mêmes noms que les paramètres de run)."""
    repo: str
    request: str
    jira_issue: str


@workflows.workflow.define(
    name="jira-watch",
    workflow_display_name="Scrutateur JIRA",
    execution_timeout=timedelta(minutes=15),
    workflow_description="Cherche les tickets JIRA « A faire » étiquetés `agent`, les prend "
                         "(« Pris en charge ») et lance le pipeline spec-to-tests sur chacun.",
)
@uses_connectors(atlassian_connector)
class JiraWatch:

    @workflows.workflow.entrypoint
    async def run(self, jql: str = "", max_starts: int = MAX_STARTS_DEFAULT,
                  dry_run: bool = False) -> dict:
        """`jql` vide = env `jira_watch_jql` (défaut : A faire + étiquette agent).
        `dry_run=True` : liste ce qui serait lancé, sans toucher aux tickets."""
        found = await jira_watch.jira_watch_candidates(jql, max(max_starts * 3, 10))
        report: dict = {"jql": found.get("jql"), "started": [], "skipped": [],
                        "dry_run": dry_run}
        if not found.get("ok"):
            report["error"] = found.get("error")
            return report

        for ticket in found.get("tickets") or []:
            key = ticket["key"]
            if len(report["started"]) >= max_starts:
                report["skipped"].append({"key": key, "reason": "quota du passage atteint"})
                continue
            if not ticket["repo"]:
                report["skipped"].append({
                    "key": key,
                    "reason": f"repo « {ticket['repo_alias']} » non résolu "
                              f"(secret gitlab_{ticket['repo_alias']}_repo absent ?)"})
                continue
            if dry_run:
                report["started"].append({"key": key, "repo": ticket["repo_alias"],
                                          "execution_id": None})
                continue

            # 1. prendre le ticket (garde v4, décision de l'agent)
            taken = await jira_tools.jira_move(key, "start", False)
            if not taken.get("ok") or taken.get("skipped"):
                report["skipped"].append({
                    "key": key,
                    "reason": ("déjà pris" if taken.get("skipped") else
                               taken.get("reason") or taken.get("error") or "non pris")})
                continue

            # 2. lancer le pipeline (enfant détaché)
            try:
                handle = await workflows.workflow.execute_workflow(
                    SpecToTests,
                    SpecToTestsInput(repo=ticket["repo"],
                                     request=jira_watch.request_for(ticket),
                                     jira_issue=key),
                    wait=False,
                )
            except Exception as exc:
                # 3. ticket pris mais pipeline non démarré : on le signale
                reason = f"démarrage du pipeline impossible : {str(exc)[:400]}"
                await jira_tools.jira_move(key, "blocked", False)
                await jira_tools.jira_comment(key, f"⛔ Scrutateur JIRA — {reason}")
                report["skipped"].append({"key": key, "reason": reason})
                continue
            report["started"].append({"key": key, "repo": ticket["repo_alias"],
                                      "execution_id": getattr(handle, "id", None)})
        return report
