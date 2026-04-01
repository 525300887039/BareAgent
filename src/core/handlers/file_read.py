from __future__ import annotations

from pathlib import Path

from src.core.sandbox import safe_path


def run_read(
    file_path: str,
    offset: int = 0,
    limit: int | None = None,
    *,
    workspace: Path,
) -> str:
    """Read a file from the workspace and prefix each line with its line number."""
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    resolved = safe_path(file_path, workspace)
    lines = resolved.read_text(encoding="utf-8").splitlines()
    end = None if limit is None else offset + limit
    selected = lines[offset:end]
    return "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(selected, start=offset + 1)
    )
