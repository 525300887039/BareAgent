"""Session-scoped tracker of recently touched files.

A tiny, pure, bounded most-recently-used list of workspace-relative file paths.
The main loop records a path whenever ``read_file`` / ``write_file`` /
``edit_file`` runs, and the ``repo_map`` tool reads the most recent entries to
bias its PageRank ranking toward what the user is actively working on (the
"automatic focus"). Keeping it a standalone pure class means the repo-map core
stays free of any session/REPL coupling -- it only ever receives the resolved
focus list by injection.

Lifecycle mirrors the other session-scoped registries (spawned agents, the
subagent registry): instantiated once per REPL, cleared on ``/new`` / ``/clear``
/ ``/resume`` / ``/import`` and preserved across ``/compact``. It is written only
from the single-threaded main loop, so it needs no lock.
"""

from __future__ import annotations

DEFAULT_CAPACITY = 20


class FileRecencyTracker:
    """A bounded MRU list of workspace-relative paths (most recent first).

    Re-recording an existing path moves it to the front (move-to-end on the
    insertion-ordered dict). When ``capacity`` is exceeded the least-recently
    used entry is dropped.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity if capacity > 0 else DEFAULT_CAPACITY
        # insertion-ordered; the LAST key is the most recent.
        self._paths: dict[str, None] = {}

    def record(self, relpath: str) -> None:
        """Record (or refresh) a path as the most recently touched."""
        if not relpath:
            return
        self._paths.pop(relpath, None)
        self._paths[relpath] = None
        while len(self._paths) > self._capacity:
            # drop the oldest (first-inserted) entry
            oldest = next(iter(self._paths))
            del self._paths[oldest]

    def recent(self, n: int | None = None) -> list[str]:
        """Return up to ``n`` paths, most recent first (all when ``n`` is None)."""
        ordered = list(reversed(self._paths.keys()))
        if n is None:
            return ordered
        if n <= 0:
            return []
        return ordered[:n]

    def clear(self) -> None:
        self._paths.clear()
