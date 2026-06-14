"""Handler + schema for the ``goal_verdict`` tool (goal-completion evaluator).

Like ``skill_create``, ``goal_verdict`` is NOT registered in the global tool set.
It is exposed only inside the isolated evaluator ``agent_loop`` call that runs
after each turn of a ``/goal`` loop (see ``main.py`` and ``src/core/goal.py``).
Keeping it out of the global set means the main loop never offers it and
sub-agents never receive it (isolation, same stance as ``skill_create`` /
``hook_engine``).

The handler is a thin shim: it parses the model-supplied fields into a
:class:`bareagent.core.goal.Verdict` (delegating coercion to ``goal.parse_verdict``)
and records it into a caller-provided ``sink`` list as a side effect, mirroring
how ``skill_create`` persists via its store. The caller reads ``sink`` after the
isolated evaluator loop returns. Returning a plain confirmation string keeps the
evaluator loop from crashing (see ``error-handling.md``).
"""

from __future__ import annotations

from bareagent.core.goal import Verdict, parse_verdict
from bareagent.core.schema import tool_schema

GOAL_VERDICT_TOOL_SCHEMA = tool_schema(
    "goal_verdict",
    (
        "Report whether the goal completion condition is now fully satisfied, "
        "judging only from the conversation. Call exactly once."
    ),
    {
        "met": {
            "type": "boolean",
            "description": "True only if the transcript verifiably satisfies the condition.",
        },
        "reason": {
            "type": "string",
            "description": (
                "Concrete justification. If not met, name what is still missing "
                "and what the agent should do next."
            ),
        },
    },
    ["met", "reason"],
)


def run_goal_verdict(
    *,
    sink: list[Verdict],
    met: object = None,
    reason: object = None,
) -> str:
    """Record the evaluator's verdict into ``sink`` and confirm to the model.

    ``sink`` is a one-element accumulator the caller reads after the isolated
    evaluator loop returns. Coercion/validation lives in ``goal.parse_verdict``,
    so this stays a thin shim that never raises.
    """
    verdict = parse_verdict({"met": met, "reason": reason})
    sink.append(verdict)
    return "Verdict recorded."
