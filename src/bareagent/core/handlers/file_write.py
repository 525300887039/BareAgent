from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from bareagent.core.sandbox import safe_path


def run_write(
    file_path: str,
    content: str,
    *,
    workspace: Path,
    diagnostics_hook: Callable[[str, Any], str | None] | None = None,
) -> str:
    """Write content to a workspace file, creating parent directories when needed.

    ``diagnostics_hook`` (Hybrid auto-diagnostics) follows the same contract
    as :func:`run_edit`: the handler snapshots before, performs the write,
    then asks the hook for an appendix. Returning ``None`` from either call
    leaves the result untouched.
    """
    resolved = safe_path(file_path, workspace)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    before = diagnostics_hook(str(resolved), None) if diagnostics_hook else None
    resolved.write_text(content, encoding="utf-8")
    relative = resolved.relative_to(workspace.resolve(strict=False))
    result = f"Wrote {len(content)} characters to {relative.as_posix()}"
    if diagnostics_hook is not None:
        appendix = diagnostics_hook(str(resolved), before)
        if appendix:
            result = f"{result}{appendix}"
    return result
