from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from bareagent.core.sandbox import safe_path


def run_edit(
    file_path: str,
    old_text: str,
    new_text: str,
    *,
    workspace: Path,
    diagnostics_hook: Callable[[str, Any], str | None] | None = None,
) -> str:
    """Replace the first matching block of text in a workspace file.

    ``diagnostics_hook`` is the Hybrid auto-diagnostics callback supplied by
    ``get_handlers`` when LSP is active. The handler invokes it before and
    after the write so any newly-introduced diagnostics can be appended to
    the tool result. The hook signature is
    ``(file_path, before) -> str | None`` — passing ``before=None`` on the
    pre-edit call lets the hook implementation produce its own snapshot,
    and ``None`` is returned whenever LSP is unavailable or the config flag
    is off so the happy path stays zero-cost.
    """
    resolved = safe_path(file_path, workspace)
    current = resolved.read_text(encoding="utf-8")
    if old_text not in current:
        raise ValueError("old_text not found in file")

    # Snapshot diagnostics before the edit so the hook can diff against the
    # post-edit state. The hook itself handles "LSP off" / "no route" cases.
    before = diagnostics_hook(str(resolved), None) if diagnostics_hook else None

    updated = current.replace(old_text, new_text, 1)
    resolved.write_text(updated, encoding="utf-8")
    relative = resolved.relative_to(workspace.resolve(strict=False))
    result = f"Edited {relative.as_posix()}"

    if diagnostics_hook is not None:
        appendix = diagnostics_hook(str(resolved), before)
        if appendix:
            result = f"{result}{appendix}"
    return result
