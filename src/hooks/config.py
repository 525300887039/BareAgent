"""Hook configuration parsing.

Reads a ``[[hooks]]`` array of tables from a TOML-derived dict and returns a
typed :class:`HooksConfig`. Each entry binds a shell ``command`` to a
:class:`~src.hooks.events.HookEvent` and an optional precise ``tool`` name
(omitted = matches every tool).

Graceful degradation policy (mirrors MCP/LSP): a structurally broken document
(non-list ``hooks``, non-table entry) raises :class:`HookConfigError` so
``main.py`` can warn and fall back to an empty config. A single *malformed*
entry (unknown event, blank command) is skipped and recorded in
:attr:`HooksConfig.skipped`, rather than nuking the whole config — one bad line
should not disable a user's other working hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import HookConfigError
from .events import HookEvent

_VALID_EVENTS = frozenset(event.value for event in HookEvent)
_DEFAULT_TIMEOUT = 30


@dataclass(slots=True)
class HookEntry:
    """One ``[[hooks]]`` entry: when to fire and what to run."""

    event: str
    command: str
    # None => match every tool for this event.
    tool: str | None = None
    timeout: int = _DEFAULT_TIMEOUT


@dataclass(slots=True)
class HooksConfig:
    """Top-level hooks configuration."""

    entries: list[HookEntry] = field(default_factory=list)
    # Human-readable reasons for entries that were dropped during parsing.
    skipped: list[str] = field(default_factory=list)

    def matching(self, event: str, tool_name: str) -> list[HookEntry]:
        """Return entries for *event* whose ``tool`` is None or equals *tool_name*.

        Order is preserved (config declaration order) so hooks fire predictably.
        """
        return [
            entry
            for entry in self.entries
            if entry.event == event and (entry.tool is None or entry.tool == tool_name)
        ]


def parse_hooks_config(raw: dict[str, Any]) -> HooksConfig:
    """Parse a TOML-derived dict into :class:`HooksConfig`.

    Accepts either the full document (where ``hooks`` is a key holding the
    array of tables) or a bare ``{"hooks": [...]}`` wrapper. Unknown keys on an
    entry are ignored to stay forward-compatible.

    Raises :class:`HookConfigError` only for structural failures (the document
    or the ``hooks`` value has the wrong shape). Individual malformed entries
    are skipped and recorded in :attr:`HooksConfig.skipped`.
    """
    if not isinstance(raw, dict):
        raise HookConfigError(f"hooks config must be a table, got {type(raw).__name__}")

    entries_raw = raw.get("hooks", [])
    if not isinstance(entries_raw, list):
        raise HookConfigError("'hooks' must be an array of tables")

    config = HooksConfig()
    for index, entry in enumerate(entries_raw):
        if not isinstance(entry, dict):
            config.skipped.append(f"hooks[{index}] is not a table; skipped")
            continue
        parsed = _parse_entry(entry, index, config.skipped)
        if parsed is not None:
            config.entries.append(parsed)
    return config


def _parse_entry(entry: dict[str, Any], index: int, skipped: list[str]) -> HookEntry | None:
    event = entry.get("event")
    if event not in _VALID_EVENTS:
        skipped.append(
            f"hooks[{index}].event must be one of {sorted(_VALID_EVENTS)}, got {event!r}; skipped"
        )
        return None

    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        skipped.append(
            f"hooks[{index}].command is required and must be a non-empty string; skipped"
        )
        return None

    tool = entry.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool):
        skipped.append(f"hooks[{index}].tool must be a non-empty string if provided; skipped")
        return None

    timeout = entry.get("timeout", _DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        skipped.append(f"hooks[{index}].timeout must be a positive integer; skipped")
        return None

    return HookEntry(
        event=str(event),
        command=command,
        tool=tool,
        timeout=timeout,
    )
