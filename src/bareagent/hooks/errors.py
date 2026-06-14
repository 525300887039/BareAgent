"""Hook error hierarchy.

Only ``HookConfigError`` is raised — at parse time, so ``main.py`` can catch it
and degrade gracefully (warn + run with an empty :class:`HooksConfig`), mirroring
the MCP/LSP config-failure fallbacks. Hook *runtime* failures (spawn error,
timeout, non-zero exit) are never exceptions: they are fail-open per PRD D3 and
surface as console warnings instead.
"""

from __future__ import annotations


class HookConfigError(Exception):
    """Raised when the ``[[hooks]]`` configuration is structurally invalid."""
