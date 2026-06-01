"""Experiential skill generation: decide when to draft a reusable skill.

Pure logic with no LLM / loop / SDK dependencies so it is unit-testable in
isolation (mirrors the ``src/core/retry.py`` pure-module pattern). The agent
loop feeds per-turn tool-call counts into :class:`SkillGenerator`, which
accumulates them across user turns and reports when both thresholds are crossed.
The actual reflection LLM call + draft persistence live in the REPL / store
layers; this module only owns the *trigger decision* and the draft instruction
text.

Design (see task 06-01-experiential-skill-gen):
- A "task worth saving" spans multiple user turns AND involves real tool work.
  So the trigger is a double AND: cumulative ``tool_calls >= min_tool_calls``
  and cumulative ``user_replies >= min_user_replies``.
- Counters accumulate from session start / last draft and reset on each trigger,
  so one multi-turn workflow is packed into a single skill rather than firing
  every turn.
- ``enabled=False`` short-circuits everything: no counting, never drafts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SkillGenConfig:
    """Thresholds + master switch for experiential skill generation.

    Mirrors the user-facing ``[skills]`` config (``main.py`` builds this from
    ``SkillsConfig`` the same way ``_build_retry_policy`` adapts ``RetryConfig``
    to ``RetryPolicy``).
    """

    enabled: bool = True
    min_tool_calls: int = 5
    min_user_replies: int = 3


def should_draft_skill(
    tool_calls: int,
    user_replies: int,
    config: SkillGenConfig,
) -> bool:
    """Return True when the accumulated activity warrants drafting a skill.

    Double AND on the two thresholds; always False when disabled. Pure function
    so the decision is directly unit-testable without constructing a generator.
    """
    if not config.enabled:
        return False
    return tool_calls >= config.min_tool_calls and user_replies >= config.min_user_replies


# Instruction injected as a user turn for the isolated reflection call. It lets
# the model DECLINE (respond "no skill") so a low-value, one-off task does not
# get forced into a skill — a second quality gate on top of the user-promote
# step. The model is told to call ``skill_create`` exactly once when it does
# decide the workflow is worth preserving.
DRAFT_INSTRUCTION = (
    "You just finished a multi-step task spanning several turns. If — and only "
    "if — there is a genuinely reusable, non-trivial workflow worth preserving "
    "for next time, capture it as a skill by calling the `skill_create` tool "
    "exactly once.\n\n"
    "Distill the procedure so a future agent with no memory of this session "
    "could follow it:\n"
    '- name: short kebab-case identifier (e.g. "add-config-section").\n'
    '- description: one line starting with "Use this when".\n'
    "- body: markdown with sections like Steps / Pitfalls / Verification, "
    "including dead-ends you hit and how you got past them.\n\n"
    "If the work was too trivial or one-off to generalize, do NOT call the "
    'tool — reply with the single line "no skill" instead.'
)


class SkillGenerator:
    """Accumulates per-turn activity and decides when to draft a skill.

    Lives for the whole REPL session and is passed into the *main* ``agent_loop``
    only (sub-agents never receive it, so background / nested agents never
    trigger generation — same isolation stance as ``hook_engine``). The loop
    calls :meth:`note_turn` once per completed user turn; the REPL then checks
    :meth:`should_draft` and runs the reflection.
    """

    __slots__ = ("config", "_tool_calls", "_user_replies")

    def __init__(self, config: SkillGenConfig) -> None:
        self.config = config
        self._tool_calls = 0
        self._user_replies = 0

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def counters(self) -> tuple[int, int]:
        """Current ``(tool_calls, user_replies)`` accumulators (for tests / logs)."""
        return (self._tool_calls, self._user_replies)

    def note_turn(self, turn_tool_calls: int) -> None:
        """Record one completed user turn that ran ``turn_tool_calls`` tools."""
        if not self.config.enabled:
            return
        self._tool_calls += max(0, int(turn_tool_calls))
        self._user_replies += 1

    def should_draft(self) -> bool:
        """Whether the accumulated activity has crossed both thresholds."""
        return should_draft_skill(self._tool_calls, self._user_replies, self.config)

    def reset(self) -> None:
        """Zero the accumulators (on trigger, or on session boundary)."""
        self._tool_calls = 0
        self._user_replies = 0
