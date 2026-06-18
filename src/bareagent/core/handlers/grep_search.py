from __future__ import annotations

import re
from pathlib import Path

from bareagent.core.handlers.search_utils import iter_search_files
from bareagent.core.sandbox import safe_path

MAX_MATCHES = 1000
MAX_FILE_SIZE = 1_048_576  # 1 MB

# Mirrors ripgrep / Claude Code's Grep tool. ``content`` is the default and the
# pre-existing behavior; the other two trade detail for far fewer tokens.
GREP_OUTPUT_MODES = frozenset({"content", "files_with_matches", "count"})


def run_grep(
    pattern: str,
    path: str = ".",
    include: str = "",
    output_mode: str = "content",
    *,
    workspace: Path,
) -> list[str]:
    """Search for a regex pattern in workspace files.

    ``output_mode`` controls how much is returned (token efficiency):
      - ``"content"`` (default): ``file:line:text`` per matching line.
      - ``"files_with_matches"``: deduplicated list of files with a match —
        the cheapest mode, for when only *which* files match is needed.
      - ``"count"``: ``file:count`` per file with at least one match.
    An unrecognized mode degrades gracefully to ``"content"``.
    """
    workspace_path = workspace.resolve(strict=False)
    search_root = safe_path(path, workspace_path)
    mode = output_mode if output_mode in GREP_OUTPUT_MODES else "content"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return [f"Invalid regex pattern: {exc}"]

    content_matches: list[str] = []
    # (file, count) in discovery order; one entry per matching file (deduped).
    file_counts: list[tuple[str, int]] = []
    total = 0
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

        rel_posix = relative.as_posix()
        file_count = 0
        for line_number, line in enumerate(lines, start=1):
            if not regex.search(line):
                continue
            file_count += 1
            total += 1
            if mode == "content":
                content_matches.append(f"{rel_posix}:{line_number}:{line}")
                if total >= MAX_MATCHES:
                    break
            elif mode == "files_with_matches":
                break  # one hit is enough to flag the file
        if file_count:
            file_counts.append((rel_posix, file_count))
        if mode == "content":
            if total >= MAX_MATCHES:
                break
        elif len(file_counts) >= MAX_MATCHES:
            break

    if mode == "files_with_matches":
        return [name for name, _ in file_counts]
    if mode == "count":
        return [f"{name}:{count}" for name, count in file_counts]
    return content_matches
