"""Envoie le signal approve/reject à une exécution du pipeline (gate humaine).

Usage :
    python scripts/approve.py <execution_id> approve
    python scripts/approve.py <execution_id> reject

L'execution_id est affiché par scripts/run.py au lancement et dans la
notification de gate (Slack...). Le workflow dort sur wait_condition et
reprend dès réception du signal.
"""

import argparse
import json
import os

from mistralai.client import Mistral


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution_id", help="ID de l'exécution (voir run.py / Studio)")
    parser.add_argument("verdict", choices=["approve", "reject"])
    args = parser.parse_args()

    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    client.workflows.executions.signal_workflow_execution(
        execution_id=args.execution_id,
        name=args.verdict,
        input={},
    )
    print(json.dumps({"sent": args.verdict, "execution_id": args.execution_id}, indent=2))


if __name__ == "__main__":
    main()
