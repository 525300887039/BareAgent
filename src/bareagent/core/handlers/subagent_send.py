from __future__ import annotations

from collections.abc import Callable

from bareagent.core.schema import tool_schema
from bareagent.planning.subagent_registry import ResumableContext, SubagentRegistry

SUBAGENT_SEND_TOOL_SCHEMA = tool_schema(
    "subagent_send",
    (
        "Continue a previously spawned foreground subagent, preserving its full "
        "context. Pass the agent id returned when the subagent was spawned plus a "
        "follow-up message; the subagent resumes its conversation and returns a "
        "new result. Only foreground, non-worktree subagents are resumable -- "
        "background and worktree-isolated subagents do not register a context."
    ),
    {
        "agent_id": {
            "type": "string",
            "description": "Id of the subagent to continue (e.g. sa-xxxxxxxx).",
        },
        "message": {
            "type": "string",
            "description": "Follow-up message to send to the subagent.",
        },
    },
    ["agent_id", "message"],
)


def _resume_footnote(agent_id: str) -> str:
    return f"\n\n[subagent id {agent_id}: still resumable -- continue with subagent_send]"


def run_subagent_send(
    agent_id: str,
    message: str,
    *,
    registry: SubagentRegistry,
    run_loop: Callable[[ResumableContext], str],
) -> str:
    """Pure-ish driver for the ``subagent_send`` tool (``run_loop`` injected).

    Validates input, looks up the resumable context, appends the follow-up user
    message, re-enters the loop via ``run_loop``, refreshes the context's
    position in the registry (so an active multi-turn conversation is not
    evicted), and returns the new result with a continuation footnote. Never
    raises on bad input or a missing id -- returns a structured ``Error:`` string
    instead.
    """
    normalized_id = agent_id.strip() if isinstance(agent_id, str) else ""
    if not normalized_id:
        return "Error: agent_id must not be empty."
    if not isinstance(message, str) or not message.strip():
        return "Error: message must not be empty."

    context = registry.get(normalized_id)
    if context is None:
        return (
            f"Error: subagent {normalized_id} not found. It may have been evicted "
            "(only the most recent foreground subagents stay resumable) or the "
            "session was reset (/new, /resume, /import, /clear)."
        )

    context.messages.append({"role": "user", "content": message})
    result = run_loop(context)
    # Re-register on success to refresh FIFO position; if run_loop raised, we
    # never get here and the context keeps its prior position.
    registry.register(context)
    return result + _resume_footnote(normalized_id)
