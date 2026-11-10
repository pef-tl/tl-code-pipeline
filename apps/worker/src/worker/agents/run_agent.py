"""Boucle d'exécution d'un agent : appels modèle + tools, bornée en tours.

Squelette volontairement explicite (sans le plugin durable déprécié) :
- l'agent est reconstruit dans l'activité (rien ne traverse Temporal),
- la boucle s'arrête à `max_turns`, réponse finale sinon,
- chaque tool call est tracé (la télémétrie Mistral ajoute le tracing).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from mistralai.client import Mistral

from worker.agents.definitions import AgentSpec, MAX_AGENT_TURNS


async def run_agent(agent: AgentSpec, user_message: str, tool_impls: dict[str, Callable]) -> str:
    """Exécute la boucle modèle -> tools -> modèle jusqu'à la réponse finale."""
    client = Mistral()  # lit MISTRAL_API_KEY

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.instructions},
        {"role": "user", "content": user_message},
    ]
    tools_schema = [
        {"type": "function", "function": {"name": name, **spec}}
        for name, (spec, _) in tool_impls.items()
    ] if tool_impls else None

    for _ in range(MAX_AGENT_TURNS):
        response = client.chat.complete(
            model=agent.model,
            messages=messages,
            tools=tools_schema,
        )
        choice = response.choices[0]
        messages.append(choice.message.model_dump())

        if not choice.message.tool_calls:
            return choice.message.content or ""

        for call in choice.message.tool_calls:
            fn = call.function
            impl = tool_impls[fn.name][1]
            try:
                result = await impl(**json.loads(fn.arguments or "{}"))
            except Exception as exc:  # l'agent voit l'erreur et corrige
                result = {"error": str(exc)}
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, default=str),
            })

    return ""  # max_turns atteint : escalade gérée par l'appelant
