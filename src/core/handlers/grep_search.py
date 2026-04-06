from __future__ import annotations

import re
from pathlib import Path

from src.core.handlers.search_utils import iter_search_files
from src.core.sandbox import safe_path


MAX_MATCHES = 1000
MAX_FILE_SIZE = 1_048_576  # 1 MB


def run_grep(
    pattern: str,
    path: str = ".",
    include: str = "",
    *,
    workspace: Path,
) -> list[str]:
    """Search for a regex pattern in workspace files."""
    workspace_path = workspace.resolve(strict=False)
    search_root = safe_path(path, workspace_path)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return [f"Invalid regex pattern: {exc}"]

    matches: list[str] = []
    for file_path in iter_search_files(search_root):
        resolved = file_path.resolve(strict=False)
        if not resolved.is_relative_to(workspace_path):
            continue

        relative = resolved.relative_to(workspace_path)
        if include and not relative.match(include):
            continue

        try:
            if resolved.stat().st_size > MAX_FILE_SIZE:
                continue
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for line_number, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append(f"{relative.as_posix()}:{line_number}:{line}")
                if len(matches) >= MAX_MATCHES:
                    break
        if len(matches) >= MAX_MATCHES:
            break
    return matches
