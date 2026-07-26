# Keep memory recall out of fork previews

## Goal

Keep `/fork` point previews anchored to genuine user prompts when persistent
memory recall injects a synthetic user-role context block.

## Requirements

- `enumerate_fork_points` must ignore reserved `<memory-recall>` messages when
  updating the latest user preview.
- Legal fork boundaries, numbering, cut positions, and sliced conversation
  content must remain unchanged.
- Ordinary string and structured-text user messages, including compaction
  summaries, must retain their existing preview behavior.
- The session-tree module must remain independent of `bareagent.main`.

## Acceptance Criteria

- [x] A cross-module regression test performs real memory recall injection and
  confirms the fork preview still shows the original user question.
- [x] The regression test confirms the recall block remains in the legal fork
  slice and the point number/cut are unchanged.
- [x] Focused memory/session-tree tests, Ruff, Pyright, and the full non-manual
  suite pass.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
