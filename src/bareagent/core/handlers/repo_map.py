"""``repo_map`` tool handler: a ranked, token-budgeted symbol skeleton.

Thin wrapper over :class:`bareagent.memory.repo_map.RepoMapIndex`. It merges the
caller's explicit ``focus`` with the automatic focus (recently read / edited
files, from the session :class:`~bareagent.core.file_recency.FileRecencyTracker`)
so the PageRank ranking foregrounds what the user is working on, scopes the
render to ``path`` (sandboxed), and formats the result. The index is fully
fail-open, so an empty map degrades to a friendly note rather than an error.
"""

from __future__ import annotations

from pathlib import Path

from bareagent.core.file_recency import FileRecencyTracker
from bareagent.core.sandbox import safe_path
from bareagent.memory.repo_map import RepoMapIndex


def run_repo_map(
    path: str = ".",
    focus: list[str] | str | None = None,
    max_tokens: int | None = None,
    *,
    index: RepoMapIndex,
    workspace: Path,
    recency_tracker: FileRecencyTracker | None = None,
    recent_files: int = 5,
) -> str:
    """Return a ranked class/function signature skeleton of the repo.

    ``path`` scopes the rendered subtree (sandboxed). ``focus`` (file paths
    and/or identifiers) is merged with the automatic recent-files focus and
    biases the PageRank ranking. ``max_tokens`` overrides the configured budget.
    """
    explicit: list[str] = []
    if isinstance(focus, str) and focus.strip():
        explicit = [focus.strip()]
    elif isinstance(focus, list):
        explicit = [item.strip() for item in focus if isinstance(item, str) and item.strip()]

    auto: list[str] = []
    if recency_tracker is not None:
        auto = recency_tracker.recent(recent_files)
    # explicit focus first; dedup while preserving order
    merged = list(dict.fromkeys([*explicit, *auto]))

    requested = path.strip() if isinstance(path, str) else "."
    if requested and requested not in (".", "./"):
        try:
            safe_path(requested, workspace.resolve(strict=False))
        except (PermissionError, ValueError):
            return f"Error: repo_map path is outside the workspace: {requested}"

    budget: int | None = None
    if max_tokens is not None:
        try:
            budget = int(max_tokens)
        except (TypeError, ValueError):
            budget = None
        if budget is not None and budget <= 0:
            budget = None

    out = index.generate(path=requested or ".", focus=merged, max_tokens=budget)
    if not out:
        return (
            "No repo map available: no supported source files found under the "
            "requested path, or the repo-map backend is unavailable. Supported "
            "languages: Python, JavaScript, Rust, Go, Java. Try `grep` or "
            "`code_search` instead."
        )
    return out
