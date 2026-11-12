"""Interroge l'état d'une exécution en cours (query `state` du workflow) :
phase courante, question en attente de l'agent, transcript de la conversation,
dernier verdict.

Usage :
    python scripts/status.py <execution_id>

Phases : init -> spec (interview agent) -> gate_spec -> plan -> dev -> merge
         -> gate_review -> done
"""

import argparse
import json
import os

from mistralai.client import Mistral


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution_id", help="ID de l'exécution (voir run.py / Studio)")
    args = parser.parse_args()

    # `mistral_api_key` en priorité ; fallback MISTRAL_API_KEY (nom réservé).
    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    resp = client.workflows.executions.query_workflow_execution(
        execution_id=args.execution_id,
        name="state",
    )
    result = resp.result
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            pass
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
