from __future__ import annotations

from pathlib import Path

from src.core.sandbox import safe_path


def run_write(file_path: str, content: str, *, workspace: Path) -> str:
    """Write content to a workspace file, creating parent directories when needed."""
    resolved = safe_path(file_path, workspace)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    relative = resolved.relative_to(workspace.resolve(strict=False))
    return f"Wrote {len(content)} characters to {relative.as_posix()}"
