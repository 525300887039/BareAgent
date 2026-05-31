"""User-defined hooks fired around tool execution in the main agent loop.

Users declare ``[[hooks]]`` in ``config.toml`` to run custom shell commands
before and after a tool call:

- ``PreToolUse`` fires after the permission check passes but before the handler
  runs. An exit code of 2 intercepts the call (the handler is skipped and the
  hook's stderr is fed back to the LLM as an error result).
- ``PostToolUse`` fires after the handler returns successfully, for side effects
  (e.g. ``ruff format`` after ``write_file``). Its exit code is ignored.

The permission guard remains the security boundary; hooks are a user-configured
convenience layer (trust-the-config), and they only fire in the main loop —
sub-agents never run hooks (isolation). Failures are fail-open.
"""

from __future__ import annotations

from .config import HookEntry, HooksConfig, parse_hooks_config
from .engine import HookEngine, HookOutcome
from .errors import HookConfigError
from .events import HookEvent

__all__ = [
    "HookConfigError",
    "HookEngine",
    "HookEntry",
    "HookEvent",
    "HookOutcome",
    "HooksConfig",
    "parse_hooks_config",
]
