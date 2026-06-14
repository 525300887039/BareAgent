"""Git worktree lifecycle helpers for sub-agent isolation.

A thin, dependency-free wrapper around the ``git worktree`` CLI so that a
sub-agent can run with all of its file operations rooted in an isolated
working tree + temp branch, leaving the parent workspace untouched. This
module knows nothing about the agent loop or LLM providers and is unit
testable against a real temporary repository.

The git subprocess style mirrors ``src/core/context.py::_run_git_command``
(cwd + capture + text + utf-8 + errors="replace" + timeout). These calls
run as infrastructure (same tier as ``tasks.py`` / ``context.py``) and are
deliberately *not* routed through ``PermissionGuard`` — the sub-agent's own
bash/write tools are still gated by its ``child_permission``.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bareagent.core.fileutil import generate_random_id

_GIT_TIMEOUT = 30


class WorktreeError(Exception):
    """Raised when creating a git worktree fails."""


@dataclass(slots=True)
class WorktreeHandle:
    """Identifies an isolated worktree created for a sub-agent."""

    path: str
    branch: str
    base_workspace: str


def _run_git(workspace: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the completed process (no ``check``).

    Callers inspect ``returncode`` and decide whether to raise or swallow —
    creation raises, cleanup is best-effort.
    """
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT,
    )


def is_git_repo(workspace: str | Path) -> bool:
    """Return ``True`` when *workspace* sits inside a git work tree."""
    try:
        completed = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def create_worktree(workspace: str | Path) -> WorktreeHandle:
    """Create an isolated worktree + temp branch for *workspace*.

    The worktree is placed under the system temp directory (outside the repo,
    so the sub-agent's glob/grep cannot scan it and it never enters the git
    index). ``mkdtemp`` pre-creates an empty directory; ``git worktree add``
    accepts an existing empty directory as its target.
    """
    worktree_id = generate_random_id(8)
    branch = f"bareagent/wt-{worktree_id}"
    path = tempfile.mkdtemp(prefix="bareagent-wt-")

    try:
        completed = _run_git(workspace, "worktree", "add", path, "-b", branch)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git worktree add failed: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise WorktreeError(f"git worktree add failed: {detail}")

    return WorktreeHandle(path=path, branch=branch, base_workspace=str(workspace))


def worktree_status(path: str | Path) -> tuple[bool, str]:
    """Return ``(dirty, summary)`` for the worktree at *path*.

    Dirty is defined as a non-empty ``git status --porcelain`` — a sub-agent's
    output is uncommitted changes (auto-commit is out of scope), so there is no
    commits-ahead comparison.
    """
    try:
        completed = _run_git(path, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return False, "status unavailable"

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    dirty = bool(lines)
    summary = f"{len(lines)} file(s) changed" if dirty else "no changes"
    return dirty, summary


def remove_worktree(handle: WorktreeHandle) -> None:
    """Remove the worktree and delete its branch (best-effort, idempotent).

    Both steps swallow failures: cleanup must never raise into the sub-agent's
    result path. A leftover worktree is visible via ``git worktree list``.
    """
    base = handle.base_workspace
    for args in (
        ("worktree", "remove", "--force", handle.path),
        ("branch", "-D", handle.branch),
    ):
        try:
            _run_git(base, *args)
        except (OSError, subprocess.SubprocessError):
            pass
