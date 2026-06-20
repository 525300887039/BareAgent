"""Tests for the recency wrapping of file handlers (core/tools.py:_with_recency).

Verifies the wrapper records touched paths into the tracker, forwards the
handler's result unchanged, and keeps the ``diagnostics_hook`` keyword
discoverable so worktree rebind still works on wrapped handlers.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from bareagent.core.file_recency import FileRecencyTracker
from bareagent.core.tools import (
    _extract_diagnostics_hook,
    _with_recency,
    rebind_workspace_handlers,
)


def test_wrapper_records_and_forwards(tmp_path: Path):
    tracker = FileRecencyTracker()
    calls = []

    def fake_read(*, file_path):
        calls.append(file_path)
        return f"contents of {file_path}"

    wrapped = _with_recency(fake_read, tracker, tmp_path)
    result = wrapped(file_path="sub/mod.py")
    assert result == "contents of sub/mod.py"  # result forwarded unchanged
    assert calls == ["sub/mod.py"]  # underlying handler still called
    assert tracker.recent() == ["sub/mod.py"]  # recorded (workspace-relative)


def test_wrapper_ignores_paths_outside_workspace(tmp_path: Path):
    tracker = FileRecencyTracker()
    wrapped = _with_recency(lambda *, file_path: "ok", tracker, tmp_path)
    wrapped(file_path=str(tmp_path.parent / "outside.py"))
    assert tracker.recent() == []


def test_diagnostics_hook_survives_recency_wrap(tmp_path: Path):
    tracker = FileRecencyTracker()
    sentinel = object()
    # mimic the real write_file partial carrying a diagnostics_hook keyword
    base = partial(lambda *, workspace, diagnostics_hook, **kw: "ok", diagnostics_hook=sentinel)
    wrapped = _with_recency(base, tracker, tmp_path)
    # directly discoverable through the wrapper
    assert _extract_diagnostics_hook(wrapped) is sentinel
    # and preserved across a worktree rebind of the wrapped handler
    rebound = rebind_workspace_handlers({"write_file": wrapped}, tmp_path / "wt")
    assert _extract_diagnostics_hook(rebound["write_file"]) is sentinel
