"""Hook event identifiers.

The MVP supports exactly two events (see PRD decision D1):

- ``PreToolUse`` — fired after the permission check passes but *before* the
  tool handler runs. A hook can intercept the tool (exit code 2).
- ``PostToolUse`` — fired after the handler returns successfully, before the
  result is wrapped back to the LLM. A hook may run side effects; its exit code
  never changes the tool result.

Values match Claude Code's hook event names so that user mental models and
config snippets carry over.
"""

from __future__ import annotations

from enum import StrEnum


class HookEvent(StrEnum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
