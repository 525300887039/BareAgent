# Prevent destructive worktree cleanup

## Goal

Prevent sub-agent worktree finalization from deleting committed work or work
whose Git state cannot be inspected reliably.

## Background

- `worktree_status()` currently treats only porcelain output as dirty and does
  not check the Git command return code (`src/bareagent/planning/worktree.py:90`).
- `_finalize_worktree()` deletes every worktree reported as clean, using forced
  worktree removal and branch deletion (`src/bareagent/planning/subagent.py:323`).
- A clean temporary branch with a new commit is therefore removed even though
  its HEAD differs from the commit from which the worktree was created.
- Git status exceptions and non-zero exits are also reported as clean, so an
  unavailable inspection can trigger the same destructive cleanup.

## Requirements

- Record the worktree's creation commit and treat a changed HEAD as work that
  must be retained and reported.
- Treat status or HEAD inspection exceptions and non-zero exits as unsafe to
  clean; cleanup decisions must fail closed.
- Preserve automatic cleanup for a successfully inspected, pristine worktree
  whose HEAD still equals its creation commit.
- Add regression tests for committed work and unavailable Git status without
  weakening the existing clean/dirty lifecycle coverage.

## Acceptance Criteria

- [x] A clean worktree with no new commits is removed as before.
- [x] A clean worktree containing a new commit is retained with its branch and
  path intact, and the result explains why it was kept.
- [x] A status exception or non-zero status result retains the worktree and does
  not call destructive cleanup.
- [x] Focused worktree tests, Ruff, formatting, Pyright, and the full non-manual
  pytest suite pass.

## Out of Scope

- Automatically merging or copying sub-agent commits into the parent branch.
- Changing worktree isolation selection or permission behavior.
