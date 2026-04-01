from __future__ import annotations

from pathlib import Path


def safe_path(path: str, workspace: Path) -> Path:
    """Resolve a path and ensure it stays within the workspace."""
    workspace_path = workspace.resolve(strict=False)
    candidate = Path(path).expanduser()
    resolved = (workspace_path / candidate).resolve(strict=False)
    if not resolved.is_relative_to(workspace_path):
        raise PermissionError(f"Path {path!r} escapes workspace {workspace_path}")
    return resolved
