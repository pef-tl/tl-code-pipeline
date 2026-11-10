"""Activity d'exécution des agents LLM (spec / planner / dev / tester / reviewer).

Un agent ne s'exécute JAMAIS dans le workflow : le sandbox Temporal interdit tout
appel réseau (client Mistral -> httpx -> truststore -> platform.system()) et tout
objet non sérialisable. Le workflow ne passe donc que des paramètres JSON
(nom de l'agent, message, kit de tools + cible) ; l'agent et ses tools sont
reconstruits ICI, dans l'activity, puis la boucle `run_agent` tourne hors sandbox.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import mistralai.workflows as workflows

from worker.activities.openspec_tools import build_spec_tools
from worker.agents import definitions as defs
from worker.agents.run_agent import run_agent
from worker.agents.tools import build_dev_tools

# Une boucle d'agent peut durer longtemps (MAX_AGENT_TURNS appels modèle + tools).
AGENT_TIMEOUT = timedelta(minutes=60)


def _tool_impls(toolkit: str, target: str) -> dict[str, tuple[dict, Callable]]:
    """Reconstruit les tools de l'agent depuis des paramètres sérialisables :
    `spec` -> tools OpenSpec du repo cible, `dev` -> tools confinés au worktree."""
    if toolkit == "spec":
        return build_spec_tools(target)
    if toolkit == "dev":
        return build_dev_tools(target)
    raise ValueError(f"toolkit inconnu: {toolkit!r} (attendu: 'spec' ou 'dev')")


@workflows.activity(start_to_close_timeout=AGENT_TIMEOUT)
async def run_agent_activity(
    agent_name: str, user_message: str, toolkit: str, target: str
) -> str:
    """Exécute l'agent `agent_name` (voir defs.AGENT_FACTORIES) avec les tools
    du kit `toolkit` appliqués à `target`, et retourne sa réponse finale."""
    try:
        factory = defs.AGENT_FACTORIES[agent_name]
    except KeyError:
        raise ValueError(
            f"agent inconnu: {agent_name!r} (attendu: {sorted(defs.AGENT_FACTORIES)})"
        )
    tool_impls: dict[str, Any] = _tool_impls(toolkit, target)
    agent = factory(tools=[impl for _, impl in tool_impls.values()])
    return await run_agent(agent, user_message, tool_impls)
