from __future__ import annotations

import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from src.core.handlers.bash import run_bash
from src.core.handlers.file_edit import run_edit
from src.core.handlers.file_read import run_read
from src.core.handlers.file_write import run_write
from src.core.handlers.glob_search import run_glob
from src.core.handlers.grep_search import run_grep
from src.core.tools import rebind_workspace_handlers
from src.planning import worktree
from src.planning.subagent import run_subagent
from src.planning.worktree import (
    create_worktree,
    is_git_repo,
    remove_worktree,
    worktree_status,
)
from src.provider.base import BaseLLMProvider, LLMResponse

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git CLI not available")


class _RecordingProvider(BaseLLMProvider):
    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        return LLMResponse(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=0,
            output_tokens=0,
        )

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def _init_repo(path: Path) -> None:
    """Initialise a git repo with one commit so worktree add can branch from HEAD."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# --- worktree.py lifecycle --------------------------------------------------


@requires_git
def test_is_git_repo_false_for_non_repo(tmp_path) -> None:
    assert is_git_repo(tmp_path) is False


@requires_git
def test_worktree_lifecycle_clean(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    assert is_git_repo(repo) is True

    handle = create_worktree(repo)
    try:
        assert Path(handle.path).is_dir()
        assert handle.branch.startswith("bareagent/wt-")
        assert handle.base_workspace == str(repo)

        dirty, summary = worktree_status(handle.path)
        assert dirty is False
        assert summary == "no changes"
    finally:
        remove_worktree(handle)

    assert not Path(handle.path).exists()


@requires_git
def test_worktree_status_dirty_after_write(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handle = create_worktree(repo)
    try:
        (Path(handle.path) / "new_file.txt").write_text("hello\n", encoding="utf-8")
        dirty, summary = worktree_status(handle.path)
        assert dirty is True
        assert "1 file(s) changed" == summary
    finally:
        remove_worktree(handle)


@requires_git
def test_remove_worktree_is_idempotent(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handle = create_worktree(repo)
    remove_worktree(handle)
    # Second removal must not raise even though nothing is left.
    remove_worktree(handle)


# --- rebind_workspace_handlers ----------------------------------------------


def _hook(file_path: str, _output) -> str | None:
    _ = file_path
    return None


def test_rebind_workspace_handlers_repoints_file_ops(tmp_path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    handlers = {
        "bash": partial(run_bash, cwd=old),
        "read_file": partial(run_read, workspace=old),
        "write_file": partial(run_write, workspace=old, diagnostics_hook=_hook),
        "edit_file": partial(run_edit, workspace=old, diagnostics_hook=_hook),
        "glob": partial(run_glob, workspace=old),
        "grep": partial(run_grep, workspace=old),
        "memory": object(),  # unrelated handler, must be preserved
    }

    rebound = rebind_workspace_handlers(handlers, new)

    assert rebound["bash"].keywords["cwd"] == new
    for key in ("read_file", "write_file", "edit_file", "glob", "grep"):
        assert rebound[key].keywords["workspace"] == new
    # diagnostics_hook preserved on write/edit
    assert rebound["write_file"].keywords["diagnostics_hook"] is _hook
    assert rebound["edit_file"].keywords["diagnostics_hook"] is _hook
    # unrelated handler untouched + originals not mutated
    assert rebound["memory"] is handlers["memory"]
    assert handlers["bash"].keywords["cwd"] == old


def test_rebind_workspace_handlers_handles_missing_hook(tmp_path) -> None:
    new = tmp_path / "new"
    handlers = {
        "bash": partial(run_bash, cwd=tmp_path),
        "read_file": partial(run_read, workspace=tmp_path),
        "write_file": object(),  # not a partial — hook extraction must not crash
        "edit_file": object(),
        "glob": partial(run_glob, workspace=tmp_path),
        "grep": partial(run_grep, workspace=tmp_path),
    }

    rebound = rebind_workspace_handlers(handlers, new)

    assert rebound["write_file"].keywords["diagnostics_hook"] is None
    assert rebound["edit_file"].keywords["diagnostics_hook"] is None


# --- run_subagent(isolation="worktree") integration -------------------------


@requires_git
def test_subagent_worktree_writes_land_in_worktree(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)

    captured: dict[str, dict[str, Any]] = {}

    def _fake_agent_loop(*, handlers, **kwargs) -> str:
        _ = kwargs
        captured["handlers"] = handlers
        # Simulate the child writing a file via its (rebound) write handler.
        handlers["write_file"](file_path="child.txt", content="from child\n")
        return "child result"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)

    result = run_subagent(
        provider=_RecordingProvider(),
        task="write a file",
        tools=[{"name": "write_file"}],
        handlers={
            "write_file": partial(run_write, workspace=repo, diagnostics_hook=None),
        },
        permission=None,
        isolation="worktree",
    )

    # The child wrote into the worktree, not the main repo.
    assert not (repo / "child.txt").exists()
    # Worktree is dirty → kept + reported.
    assert "[worktree] kept at" in result
    assert "bareagent/wt-" in result
    assert result.startswith("child result")

    # The rebound handler points at the worktree, where the file landed.
    write_handler = captured["handlers"]["write_file"]
    wt_path = Path(write_handler.keywords["workspace"])
    assert (wt_path / "child.txt").exists()

    # Clean up the kept worktree so the temp dir does not leak.
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True,
    )


@requires_git
def test_subagent_worktree_clean_is_removed(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)

    captured: dict[str, dict[str, Any]] = {}

    def _fake_agent_loop(*, handlers, **kwargs) -> str:
        _ = kwargs
        captured["handlers"] = handlers
        return "no writes"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)

    result = run_subagent(
        provider=_RecordingProvider(),
        task="do nothing to disk",
        tools=[{"name": "write_file"}],
        handlers={
            "write_file": partial(run_write, workspace=repo, diagnostics_hook=None),
        },
        permission=None,
        isolation="worktree",
    )

    assert "[worktree] cleaned up (no changes)" in result
    wt_path = Path(captured["handlers"]["write_file"].keywords["workspace"])
    assert not wt_path.exists()


def test_subagent_worktree_fail_open_for_non_repo(tmp_path, monkeypatch) -> None:
    # No git init → not a repo. Must fall back to no isolation with a footnote.
    monkeypatch.chdir(tmp_path)

    def _fake_agent_loop(**kwargs) -> str:
        _ = kwargs
        return "ran anyway"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)
    # Force is_git_repo False regardless of any ambient parent repo.
    monkeypatch.setattr(worktree, "is_git_repo", lambda _base: False)
    monkeypatch.setattr("src.planning.subagent.is_git_repo", lambda _base: False)

    result = run_subagent(
        provider=_RecordingProvider(),
        task="anything",
        tools=[{"name": "write_file"}],
        handlers={"write_file": partial(run_write, workspace=tmp_path, diagnostics_hook=None)},
        permission=None,
        isolation="worktree",
    )

    assert result.startswith("ran anyway")
    assert "[worktree] skipped: not a git repository" in result


def test_subagent_worktree_fail_open_on_create_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def _fake_agent_loop(**kwargs) -> str:
        _ = kwargs
        return "ran anyway"

    def _boom(_base):
        raise worktree.WorktreeError("boom")

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)
    monkeypatch.setattr("src.planning.subagent.is_git_repo", lambda _base: True)
    monkeypatch.setattr("src.planning.subagent.create_worktree", _boom)

    result = run_subagent(
        provider=_RecordingProvider(),
        task="anything",
        tools=[{"name": "write_file"}],
        handlers={"write_file": partial(run_write, workspace=tmp_path, diagnostics_hook=None)},
        permission=None,
        isolation="worktree",
    )

    assert result.startswith("ran anyway")
    assert "[worktree] skipped: boom" in result
