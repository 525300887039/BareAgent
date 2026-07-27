# Harden composed shell command permissions

## Goal

Prevent compound shell syntax, nested Windows shell launchers, and valid Unix
quote concatenation from bypassing permission confirmation.

## Requirements

- DEFAULT mode must not classify an entire compound command as read-only merely
  because its first command matches a safe Git command or another auto-safe
  command prefix. Compound syntax includes command separators, unquoted
  newlines, redirection, grouping expressions, and shell command substitution.
- AUTO mode and `is_dangerous` must recognize opaque Windows shell launchers
  that can hide destructive commands, including PowerShell `-Command` and
  `cmd /c`, consistently with the existing fail-closed treatment of `sh -c`.
- Valid Unix quote concatenation must not hide the Git executable, subcommand,
  or destructive option from classification; valid shell escape and line
  continuation forms must not hide them either.
- Direct, single-command safe Git invocations and existing non-Git auto-safe
  commands must retain their DEFAULT-mode behavior.
- Dangerous classification must continue to precede allow rules; PLAN,
  BYPASS, MCP, and fail-closed behavior must remain unchanged.
- Tests must classify command strings only and must never execute destructive
  shell or Git commands.

## Acceptance Criteria

- [x] Regression tests cover safe-prefix chaining/newlines/redirection/grouping/
  command substitution, quoted PowerShell and cmd wrappers around destructive
  Git, and Unix quote/escape concatenation in the executable, subcommand, and
  force option.
- [x] The regressions require confirmation in the relevant mode and destructive
  Git/wrapper cases are reported by `is_dangerous`.
- [x] Safe single-command near-neighbors remain unprompted and non-dangerous.
- [x] Focused permission tests, Ruff, Pyright, and the full non-manual test suite
  pass.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
