# Prevent ghost state after persistence failures

## Goal

Keep the task and teammate managers' in-memory state consistent with their
last successfully persisted state when an atomic JSON write fails.

## Requirements

- `TaskManager.create` must not retain a newly created task when persistence
  fails.
- `TaskManager.update` must validate every proposed field before changing live
  state and must restore every changed field, including `updated_at`, when
  persistence fails.
- `TeammateManager.register` must not retain a new teammate or overwrite an
  existing teammate when persistence fails.
- The original persistence exception must continue to propagate to the
  caller; failed operations must not report success.
- Successful mutation behavior and the on-disk JSON formats must remain
  unchanged.

## Acceptance Criteria

- [x] Regression tests force each affected save path to fail and confirm that
  both in-memory state and a freshly reloaded manager retain the prior state.
- [x] New-task creation, task status/title updates, new teammate registration,
  and replacement teammate registration are covered.
- [x] A later successful save cannot persist state from an earlier failed
  operation, and a combined valid-status/invalid-title update is atomic.
- [x] Focused tests, Ruff, Pyright, and the full non-manual test suite pass.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
