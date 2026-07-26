# Harden Git permission classification

## Goal

Prevent destructive Git commands from bypassing permission confirmation when
valid shell syntax changes the executable spelling, quoting, or placement of
Git global options.

## Requirements

- Shell permission checks must recognize `git` and `git.exe`, including quoted
  or absolute executable paths and PowerShell's call operator.
- Git global options such as `-C`, `-c`, and `--no-pager` may precede the
  subcommand without hiding it from classification.
- Forced pushes, forced cleans, hard resets, and destructive branch deletion
  must require confirmation in AUTO mode and must be reported by
  `is_dangerous`, regardless of allow rules or background fail-closed use.
- Read-only Git commands already treated as safe must retain that behavior for
  the supported invocation variants in DEFAULT mode.
- Nearby safe inputs such as `git clean -n` and a ref ending in `-f` must not be
  classified as dangerous.
- Other permission modes, MCP handling, and non-Git dangerous patterns must
  remain unchanged.

## Acceptance Criteria

- [x] Parameterized regression tests cover executable suffix/path variants,
  quotes, global options, bundled force flags, allow rules, and fail-closed
  guards without executing a destructive command.
- [x] Safe Git variants and safe near-neighbors remain unprompted or
  non-dangerous as appropriate.
- [x] Focused permission tests, Ruff, Pyright, and the full non-manual test
  suite pass.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
