"""Handler + schema for the ``exit_plan_mode`` tool (plan-mode workflow).

Like ``skill_create``, ``exit_plan_mode`` is NOT registered in the global tool
set (``core/tools.py``). It is injected only into the *main* REPL loop's tool
list in ``main.py`` after the base handlers are built. Keeping it out of
``get_tools()`` / the base handler dict means sub-agents never receive it:
``run_subagent`` filters tools by the base schema list (which lacks it) and
``filter_handlers`` then drops the orphaned handler. A sub-agent must never be
able to flip the parent's permission mode.

The handler is pure: it validates the plan, delegates the user interaction and
the permission-mode flip to an injected ``approve_fn`` (wired in ``main.py``
where the UI console and ``PermissionGuard`` live), and maps the resulting
decision to an ``Error:``-or-instruction string the LLM reads to decide what to
do next. This keeps ``core/handlers`` free of any dependency on ``permission`` /
``ui`` and makes the mapping unit-testable with a fake ``approve_fn``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bareagent.core.schema import tool_schema

EXIT_PLAN_MODE_TOOL_SCHEMA = tool_schema(
    "exit_plan_mode",
    (
        "Present your completed implementation plan for user approval and leave "
        "plan mode. Call this only after researching the task with read-only "
        "tools. On approval the permission mode switches and you continue with "
        "the implementation; on rejection you stay in plan mode and revise."
    ),
    {
        "plan": {
            "type": "string",
            "description": (
                "The implementation plan as markdown: what you will change, in "
                "what order, and any risks. Concise but complete."
            ),
        },
    },
    ["plan"],
)


@dataclass(frozen=True, slots=True)
class PlanDecision:
    """Outcome of the plan-approval interaction.

    ``outcome`` is one of:

    - ``"approve-default"`` -- user approved; mode flipped to DEFAULT.
    - ``"approve-auto"`` -- user approved with auto-accept; mode flipped to AUTO.
    - ``"reject"`` -- user rejected; ``reason`` carries optional feedback.
    - ``"noop"`` -- called while not in plan mode (defensive; nothing happened).
    - ``"unavailable"`` -- no interactive approval possible (non-tty); stayed in plan.
    """

    outcome: str
    reason: str = ""


def run_exit_plan_mode(
    *,
    plan: str | None = None,
    approve_fn: Callable[[str], PlanDecision],
) -> str:
    if not plan or not str(plan).strip():
        return "Error: exit_plan_mode requires a non-empty 'plan'."

    decision = approve_fn(str(plan))

    if decision.outcome == "approve-default":
        return (
            "Plan approved. Permission mode is now DEFAULT (write operations are "
            "still confirmed individually). Proceed with the implementation."
        )
    if decision.outcome == "approve-auto":
        return (
            "Plan approved with auto-accept. Permission mode is now AUTO (safe "
            "commands run without prompts; dangerous ones are still blocked). "
            "Proceed with the implementation."
        )
    if decision.outcome == "unavailable":
        return (
            "Plan approval is unavailable in a non-interactive environment. Staying in plan mode."
        )
    if decision.outcome == "noop":
        return (
            "Error: exit_plan_mode is only valid in plan mode, and you are not "
            "currently in plan mode."
        )
    # Reject is the safety-net default for any unexpected outcome: staying in
    # plan mode is the conservative choice (never auto-grant write access).
    reason = decision.reason.strip()
    if reason:
        return (
            f"The user rejected the plan. Reason: {reason}\n"
            "You are still in plan mode. Revise the plan to address this feedback "
            "and call exit_plan_mode again."
        )
    return (
        "The user rejected the plan. You are still in plan mode. Revise the plan "
        "and call exit_plan_mode again."
    )
