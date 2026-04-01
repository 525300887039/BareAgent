from __future__ import annotations

from pathlib import Path

from src.core.sandbox import safe_path


def run_edit(
    file_path: str,
    old_text: str,
    new_text: str,
    *,
    workspace: Path,
) -> str:
    """Replace the first matching block of text in a workspace file."""
    resolved = safe_path(file_path, workspace)
    current = resolved.read_text(encoding="utf-8")
    if old_text not in current:
        raise ValueError("old_text not found in file")

    updated = current.replace(old_text, new_text, 1)
    resolved.write_text(updated, encoding="utf-8")
    relative = resolved.relative_to(workspace.resolve(strict=False))
    return f"Edited {relative.as_posix()}"
