"""Unit tests for the repo_map tool handler (core/handlers/repo_map.py).

A stub index records the kwargs the handler passes so focus merging, path
sandboxing, budget handling, and the empty-result message are verified without
tree-sitter.
"""

from __future__ import annotations

from pathlib import Path

from bareagent.core.file_recency import FileRecencyTracker
from bareagent.core.handlers.repo_map import run_repo_map


class _StubIndex:
    def __init__(self, output: str = "MAP") -> None:
        self.output = output
        self.calls: list[dict] = []

    def generate(self, *, path=".", focus=(), max_tokens=None) -> str:
        self.calls.append({"path": path, "focus": list(focus), "max_tokens": max_tokens})
        return self.output


def test_handler_merges_explicit_and_auto_focus(tmp_path: Path):
    index = _StubIndex()
    tracker = FileRecencyTracker()
    tracker.record("recent.py")
    out = run_repo_map(
        focus=["explicit.py"],
        index=index,
        workspace=tmp_path,
        recency_tracker=tracker,
        recent_files=5,
    )
    assert out == "MAP"
    # explicit focus first, then auto recency, deduped
    assert index.calls[0]["focus"] == ["explicit.py", "recent.py"]


def test_handler_dedups_focus(tmp_path: Path):
    index = _StubIndex()
    tracker = FileRecencyTracker()
    tracker.record("dup.py")
    run_repo_map(
        focus=["dup.py"],
        index=index,
        workspace=tmp_path,
        recency_tracker=tracker,
    )
    assert index.calls[0]["focus"] == ["dup.py"]


def test_handler_accepts_string_focus(tmp_path: Path):
    index = _StubIndex()
    run_repo_map(focus="single.py", index=index, workspace=tmp_path)
    assert index.calls[0]["focus"] == ["single.py"]


def test_handler_rejects_path_outside_workspace(tmp_path: Path):
    index = _StubIndex()
    out = run_repo_map(path="../escape", index=index, workspace=tmp_path)
    assert out.startswith("Error:")
    assert not index.calls  # never reached the index


def test_handler_passes_max_tokens(tmp_path: Path):
    index = _StubIndex()
    run_repo_map(max_tokens=256, index=index, workspace=tmp_path)
    assert index.calls[0]["max_tokens"] == 256


def test_handler_invalid_max_tokens_falls_back_to_none(tmp_path: Path):
    index = _StubIndex()
    run_repo_map(max_tokens="oops", index=index, workspace=tmp_path)
    assert index.calls[0]["max_tokens"] is None


def test_handler_empty_result_is_friendly(tmp_path: Path):
    index = _StubIndex(output="")
    out = run_repo_map(index=index, workspace=tmp_path)
    assert "No repo map available" in out
    assert "grep" in out or "code_search" in out


def test_handler_without_tracker_uses_only_explicit_focus(tmp_path: Path):
    index = _StubIndex()
    run_repo_map(focus=["x.py"], index=index, workspace=tmp_path, recency_tracker=None)
    assert index.calls[0]["focus"] == ["x.py"]
