"""Handler for the single ``memory`` tool.

Thin dispatcher over :class:`bareagent.memory.persistent.MemoryManager`. It validates
per-command required arguments and converts the manager's stdlib exceptions
into ``Error:`` strings so the LLM can read and react to failures instead of
crashing the agent loop (see ``.trellis/spec/backend/error-handling.md``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bareagent.memory.persistent import MemoryManager

# Predictable, LLM-facing failures. Anything outside this set is a real bug and
# is allowed to propagate to the loop's blanket safety net.
_HANDLED_ERRORS = (
    FileNotFoundError,
    PermissionError,
    ValueError,
    IsADirectoryError,
    NotADirectoryError,
    OSError,
)


def run_memory(
    *,
    manager: MemoryManager,
    command: str,
    path: str | None = None,
    file_text: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    insert_text: str | None = None,
    old_path: str | None = None,
    new_path: str | None = None,
    view_range: list[int] | None = None,
) -> str:
    cmd = (command or "").strip()
    try:
        if cmd == "view":
            return manager.view(path or ".", view_range=view_range)
        if cmd == "create":
            if path is None or file_text is None:
                return "Error: create requires 'path' and 'file_text'."
            return manager.create(path, file_text)
        if cmd == "str_replace":
            if path is None or old_str is None or new_str is None:
                return "Error: str_replace requires 'path', 'old_str', and 'new_str'."
            return manager.str_replace(path, old_str, new_str)
        if cmd == "insert":
            if path is None or insert_line is None or insert_text is None:
                return "Error: insert requires 'path', 'insert_line', and 'insert_text'."
            return manager.insert(path, int(insert_line), insert_text)
        if cmd == "delete":
            if path is None:
                return "Error: delete requires 'path'."
            return manager.delete(path)
        if cmd == "rename":
            if old_path is None or new_path is None:
                return "Error: rename requires 'old_path' and 'new_path'."
            return manager.rename(old_path, new_path)
    except _HANDLED_ERRORS as exc:
        return f"Error: {exc}"
    return (
        f"Error: unknown memory command {command!r}. "
        "Valid commands: view, create, str_replace, insert, delete, rename."
    )
