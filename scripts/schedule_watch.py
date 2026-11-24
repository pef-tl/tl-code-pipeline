"""Planifie (ou met à jour) le scrutateur JIRA `jira-watch` dans Mistral Workflows.

Usage :
    python scripts/schedule_watch.py                       # toutes les 10 min, lun-ven 8h-19h (Paris)
    python scripts/schedule_watch.py --cron "*/5 8-19 * * 1-5" --max-starts 1
    python scripts/schedule_watch.py --dry-run             # le scrutateur liste sans rien lancer
    python scripts/schedule_watch.py --pause | --resume | --delete
    python scripts/schedule_watch.py --once [--dry-run]    # un seul passage, tout de suite

Le scrutateur prend les tickets « A faire » étiquetés `agent` (JQL : env
`jira_watch_jql` du worker, ou --jql ici) et lance spec-to-tests sur chacun.
Résultat de chaque passage : Studio > Workflows > jira-watch > exécutions.
"""

import argparse
import json
import os

from mistralai.client import Mistral

SCHEDULE_ID = "jira-watch"
WORKFLOW = "jira-watch"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cron", default="*/10 8-19 * * 1-5",
                        help="expression cron (défaut : toutes les 10 min, lun-ven 8h-19h)")
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--jql", default="", help="JQL (vide = env jira_watch_jql du worker)")
    parser.add_argument("--max-starts", type=int, default=2,
                        help="pipelines lancés au plus par passage (défaut 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="lister les tickets qui seraient pris, sans les toucher")
    parser.add_argument("--deployment", default=os.environ.get("pipeline_deployment_name", ""),
                        help="nom du déploiement worker (optionnel)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="un passage immédiat, sans planification")
    action.add_argument("--pause", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    schedules = client.workflows.schedules
    watch_input = {"jql": args.jql, "max_starts": args.max_starts, "dry_run": args.dry_run}

    if args.once:
        execution = client.workflows.execute_workflow(
            workflow_identifier=WORKFLOW, input=watch_input, wait_for_result=False)
        print(json.dumps({"execution_id": execution.execution_id, "input": watch_input},
                         indent=2, ensure_ascii=False))
        return
    if args.pause:
        print(schedules.pause_schedule(schedule_id=SCHEDULE_ID))
        return
    if args.resume:
        print(schedules.resume_schedule(schedule_id=SCHEDULE_ID))
        return
    if args.delete:
        print(schedules.unschedule_workflow(schedule_id=SCHEDULE_ID))
        return

    schedule = {"input": watch_input, "cron_expressions": [args.cron],
                "time_zone_name": args.timezone}
    extra = {"deployment_name": args.deployment} if args.deployment else {}
    try:
        out = schedules.schedule_workflow(schedule=schedule, workflow_identifier=WORKFLOW,
                                          schedule_id=SCHEDULE_ID, **extra)
        action_done = "created"
    except Exception as exc:
        if "exist" not in str(exc).lower():
            raise
        # Planification déjà présente : on la remplace (même identifiant).
        schedules.unschedule_workflow(schedule_id=SCHEDULE_ID)
        out = schedules.schedule_workflow(schedule=schedule, workflow_identifier=WORKFLOW,
                                          schedule_id=SCHEDULE_ID, **extra)
        action_done = "replaced"
    print(json.dumps({"action": action_done, "schedule_id": SCHEDULE_ID, "cron": args.cron,
                      "timezone": args.timezone, "input": watch_input,
                      "response": str(out)[:500]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
