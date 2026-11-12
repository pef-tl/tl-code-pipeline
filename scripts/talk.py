"""Envoie un message libre à l'agent en cours dans une exécution (interview spec).

Usage :
    python scripts/talk.py <execution_id> "ta réponse à la question de l'agent"

Le workflow reçoit le signal `reply`, l'ajoute au transcript et relance l'agent
avec ta réponse. Voir l'état / le transcript : python scripts/status.py <execution_id>
"""

import argparse
import json
import os

from mistralai.client import Mistral


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution_id", help="ID de l'exécution (voir run.py / Studio)")
    parser.add_argument("text", nargs="+", help="message transmis à l'agent")
    args = parser.parse_args()

    # `mistral_api_key` en priorité ; fallback MISTRAL_API_KEY (nom réservé).
    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    client.workflows.executions.signal_workflow_execution(
        execution_id=args.execution_id,
        name="reply",
        input={"text": " ".join(args.text)},
    )
    print(json.dumps({"sent": "reply", "execution_id": args.execution_id,
                      "text": " ".join(args.text)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
