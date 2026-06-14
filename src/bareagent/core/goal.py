"""Goal completion loop: drive turns until an evaluator judges a condition met.

Pure logic with no LLM / loop / REPL / SDK dependencies, so the loop driver,
verdict parsing, prompt construction, and command parsing are fully unit-testable
with injected callbacks (mirrors the ``src/core/retry.py`` and
``src/planning/skill_gen.py`` pure-module pattern).

Division of labor (see task 06-06-goal-completion-loop):
- This module owns the *control flow*: how prompts are sequenced, when to stop,
  and why (``run_goal_loop``), plus the pure text/parse helpers.
- The REPL (``main.py``) owns the side-effecting parts: running the real
  ``agent_loop`` turn and the isolated evaluator LLM call, which it injects into
  :func:`run_goal_loop` as the ``run_turn`` / ``evaluate`` callbacks.

The loop is *synchronous and non-persistent*: ``/goal <condition>`` blocks the
REPL, drives turns until the condition is met or ``max_turns`` is hit, then
returns. There is no cross-input goal state (persistence is out of scope), so the
driver only needs the condition and the turn budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

DEFAULT_MAX_TURNS = 25

GOAL_USAGE = (
    "Usage: /goal [--max-turns N] <completion condition>\n"
    "  Drives the agent turn-after-turn until an independent evaluator judges "
    "the condition met (or the turn budget is exhausted).\n"
    "  Example: /goal all tests in tests/test_goal.py pass and ruff is clean\n"
    "  The evaluator judges only from the transcript, so state a check the agent "
    "can show evidence for (e.g. run pytest and include the exit code)."
)


class GoalOutcome(Enum):
    """Why :func:`run_goal_loop` stopped. ABORTED is set by the caller (the loop
    itself only returns MET / MAX_TURNS; interrupts propagate out of the injected
    callbacks for the caller to translate)."""

    MET = "met"
    MAX_TURNS = "max_turns"
    ABORTED = "aborted"


@dataclass(slots=True)
class GoalState:
    """Runtime state for one ``/goal`` invocation."""

    condition: str
    max_turns: int = DEFAULT_MAX_TURNS
    turns_used: int = 0


@dataclass(slots=True)
class Verdict:
    """An evaluator's judgement on whether the condition is satisfied.

    ``malformed`` flags a verdict that the evaluator failed to produce cleanly
    (LLM error, no tool call, missing field). A malformed verdict is always
    treated as *not met* so the loop falls through to its ``max_turns`` guard
    instead of crashing or stopping early.
    """

    met: bool
    reason: str = ""
    malformed: bool = False


@dataclass(slots=True)
class GoalCommand:
    """Parsed ``/goal`` command. ``action`` is ``"run" | "usage" | "error"``."""

    action: str
    condition: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    message: str = ""  # usage / error text for the non-run actions


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def parse_verdict(tool_input: dict | None) -> Verdict:
    """Coerce a ``goal_verdict`` tool input into a :class:`Verdict` defensively.

    A missing/absent ``met`` field yields a malformed (= not met) verdict rather
    than guessing, so a confused evaluator never accidentally reports success.
    """
    if not isinstance(tool_input, dict):
        return Verdict(met=False, reason="", malformed=True)
    reason = str(tool_input.get("reason", "") or "").strip()
    if "met" not in tool_input or tool_input.get("met") is None:
        return Verdict(met=False, reason=reason, malformed=True)
    return Verdict(met=_coerce_bool(tool_input.get("met")), reason=reason)


def build_initial_prompt(condition: str) -> str:
    """User message that kicks off the self-driving loop (turn 1)."""
    return (
        "Work autonomously toward the goal below until it is fully satisfied. "
        "After each step an independent evaluator checks whether the condition is "
        "met and tells you what is still missing.\n\n"
        f"<goal-condition>\n{condition.strip()}\n</goal-condition>\n\n"
        "Make concrete progress now. When you believe the condition is met, run "
        "the relevant checks and include their output so it can be verified from "
        "this conversation."
    )


def build_evaluator_prompt(condition: str) -> str:
    """User message appended to the transcript COPY for the isolated evaluator."""
    return (
        "You are a strict goal-completion evaluator. The conversation above shows "
        "an agent working toward this completion condition:\n\n"
        f"<goal-condition>\n{condition.strip()}\n</goal-condition>\n\n"
        "Judge ONLY from the conversation above whether the condition is now fully "
        "satisfied. Do not assume work that is not shown: if success is claimed but "
        "the supporting evidence (tool results, command output) is not present in "
        "the transcript, treat it as NOT met.\n\n"
        "Call the `goal_verdict` tool exactly once:\n"
        "- met=true only if the condition is fully and verifiably satisfied.\n"
        "- met=false otherwise, with a concrete `reason` naming what is still "
        "missing and what the agent should do next.\n"
        "Output nothing else."
    )


def build_continuation_prompt(reason: str) -> str:
    """User message fed back to the main loop when the goal is not yet met."""
    base = "The goal is not yet satisfied."
    reason = (reason or "").strip()
    if reason:
        base += f" Evaluator feedback: {reason}"
    return base + " Keep working toward the goal."


def parse_goal_command(rest: str, *, default_max_turns: int = DEFAULT_MAX_TURNS) -> GoalCommand:
    """Parse the text after ``/goal`` into a :class:`GoalCommand`.

    Forms: ``""`` -> usage; ``--max-turns N <condition>`` -> run with override;
    ``<condition>`` -> run with the default budget. Pure (no I/O) so it is
    directly unit-testable.
    """
    rest = (rest or "").strip()
    if not rest:
        return GoalCommand(action="usage", message=GOAL_USAGE)

    max_turns = default_max_turns
    if rest.startswith("--max-turns"):
        parts = rest.split(None, 2)  # ["--max-turns", "N", "<condition...>"]
        if len(parts) < 3:
            return GoalCommand(action="error", message="Usage: /goal [--max-turns N] <condition>")
        try:
            max_turns = int(parts[1])
        except ValueError:
            return GoalCommand(
                action="error",
                message=f"Invalid --max-turns value: {parts[1]!r} (expected an integer).",
            )
        if max_turns < 1:
            return GoalCommand(action="error", message="--max-turns must be >= 1.")
        condition = parts[2].strip()
    else:
        condition = rest

    if not condition:
        return GoalCommand(
            action="error", message="Provide a completion condition: /goal <condition>"
        )
    return GoalCommand(action="run", condition=condition, max_turns=max_turns)


def run_goal_loop(
    state: GoalState,
    *,
    run_turn: Callable[[str], None],
    evaluate: Callable[[], Verdict],
    on_progress: Callable[[str], None] | None = None,
) -> tuple[GoalOutcome, Verdict | None]:
    """Drive turns until the condition is met or the turn budget is exhausted.

    - ``run_turn(prompt)`` runs one real agent turn (appends ``prompt`` as a user
      message and runs ``agent_loop`` to completion). It owns its own rollback on
      failure and may raise (e.g. ``LLMCallError`` / ``KeyboardInterrupt``); such
      exceptions propagate out of this function for the caller to treat as
      ``ABORTED``.
    - ``evaluate()`` runs the isolated evaluator and returns a :class:`Verdict`.
      It must NOT raise for ordinary evaluator failures (return a malformed,
      not-met verdict instead); only a user interrupt should propagate.

    Returns ``(outcome, last_verdict)``. ``last_verdict`` is ``None`` only if the
    loop never ran (``max_turns < 1``, which the command parser already rejects).
    """
    last: Verdict | None = None
    prompt = build_initial_prompt(state.condition)
    while state.turns_used < state.max_turns:
        state.turns_used += 1
        if on_progress is not None:
            on_progress(f"Goal turn {state.turns_used}/{state.max_turns}...")
        run_turn(prompt)
        verdict = evaluate()
        last = verdict
        if verdict.met:
            return GoalOutcome.MET, verdict
        prompt = build_continuation_prompt(verdict.reason)
    return GoalOutcome.MAX_TURNS, last
