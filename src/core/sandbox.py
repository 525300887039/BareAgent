from __future__ import annotations

from pathlib import Path


def safe_path(path: str, workspace: Path) -> Path:
    """Resolve a path and ensure it stays within the workspace."""
    if path.startswith("~"):
        raise PermissionError(f"Home-relative paths are not allowed: {path!r}")
    workspace_path = workspace.resolve(strict=False)
    candidate = Path(path)
    if candidate.is_absolute():
        raise PermissionError(f"Absolute paths are not allowed: {path!r}")
    resolved = (workspace_path / candidate).resolve(strict=False)
    if not resolved.is_relative_to(workspace_path):
        raise PermissionError(f"Path {path!r} escapes workspace {workspace_path}")
    _check_no_symlink_in_chain(workspace_path, candidate)
    return resolved


def _check_no_symlink_in_chain(workspace: Path, candidate: Path) -> None:
    """Walk each component of *candidate* under *workspace* and reject symlinks."""
    current = workspace
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise PermissionError(f"Symlink detected in path chain: {current}")
