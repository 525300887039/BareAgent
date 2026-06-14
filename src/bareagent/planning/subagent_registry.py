from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bareagent.core.fileutil import generate_random_id

_ID_PREFIX = "sa-"
DEFAULT_MAX_RESUMABLE = 20


@dataclass(slots=True)
class ResumableContext:
    """A foreground subagent's runtime, captured so it can re-enter ``agent_loop``.

    ``messages`` is the live conversation list that ``agent_loop`` appends to in
    place, so a resumed context stays current without being re-stored. The
    remaining fields are exactly the bindings ``agent_loop`` needs to resume the
    same isolated child (provider / tools / handlers / permission / compactor /
    turn budget / retry policy).
    """

    agent_id: str
    messages: list[dict[str, Any]]
    provider: Any
    tools: list[dict[str, Any]]
    handlers: dict[str, Any]
    permission: Any
    compact_fn: Any
    max_turns: int
    retry_policy: Any = None


class SubagentRegistry:
    """Session-scoped, in-memory store of resumable foreground subagents.

    Insertion-ordered: ``register`` moves an existing id to the end (most-recently
    touched) so the FIFO eviction of the oldest entry never drops a context that
    is part of an active multi-turn conversation. Holds at most ``max_resumable``
    contexts; registering past the cap evicts the oldest. This mirrors the
    session-scoped lifecycle of ``spawned_agents`` -- the REPL calls ``clear`` on
    ``/new`` / ``/resume`` / ``/import`` / ``/clear`` and leaves it intact across
    ``/compact``.
    """

    def __init__(self, max_resumable: int = DEFAULT_MAX_RESUMABLE) -> None:
        self._max = max_resumable if max_resumable > 0 else DEFAULT_MAX_RESUMABLE
        self._contexts: dict[str, ResumableContext] = {}

    def generate_id(self) -> str:
        """Return a fresh, unused ``sa-<rand8>`` id."""
        while True:
            candidate = _ID_PREFIX + generate_random_id(8)
            if candidate not in self._contexts:
                return candidate

    def register(self, context: ResumableContext) -> None:
        """Store *context*, refreshing its position and evicting the oldest if over cap."""
        # pop + re-insert => most-recently touched moves to the end, so the
        # oldest-by-touch entry is the one evicted when we exceed the cap.
        self._contexts.pop(context.agent_id, None)
        self._contexts[context.agent_id] = context
        while len(self._contexts) > self._max:
            oldest = next(iter(self._contexts))
            del self._contexts[oldest]

    def get(self, agent_id: str) -> ResumableContext | None:
        return self._contexts.get(agent_id)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._contexts

    def clear(self) -> None:
        self._contexts.clear()

    def __len__(self) -> int:
        return len(self._contexts)
