from __future__ import annotations

from pathlib import Path

from src.core.sandbox import safe_path
from src.core.handlers.search_utils import (
    is_ignored_descendant,
    iter_search_files,
    matches_glob_pattern,
    requires_recursive_walk,
)


def run_glob(pattern: str, path: str = ".", *, workspace: Path) -> list[str]:
    """Return workspace-relative paths that match a glob pattern."""
    workspace_path = workspace.resolve(strict=False)
    search_root = safe_path(path, workspace_path)

    if search_root.is_file():
        candidates = [search_root]
    elif requires_recursive_walk(pattern):
        candidates = list(iter_search_files(search_root))
    else:
        candidates = sorted(
            candidate
            for candidate in search_root.iterdir()
            if candidate.is_file() and not is_ignored_descendant(candidate, search_root)
        )

    matches: list[str] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(workspace_path):
            continue
        if not matches_glob_pattern(resolved, search_root, pattern):
            continue
        matches.append(resolved.relative_to(workspace_path).as_posix())
    return sorted(dict.fromkeys(matches))
