# Database Guidelines

> **BareAgent has no database.** This file is kept only because Trellis's
> default backend manifest references it. Read the real persistence rules
> in [`state-persistence.md`](./state-persistence.md).

---

## Why this file is empty

BareAgent is a pure Python terminal agent that runs in a single process. State
is stored in local files (JSONL mailboxes, JSON snapshots, sequenced log
directories) — there is no SQL, no ORM, no migrations.

If a future feature genuinely needs a database, **do not** silently introduce
SQLAlchemy / SQLite here. Open a design discussion first; persistence choices
in this project are intentional (zero external deps, append-only durability,
trivial inspection with `cat` / `jq`).

For all current state-handling rules — JSONL append patterns, atomic JSON
writes, session-ID conventions, config layering — see
[`state-persistence.md`](./state-persistence.md).
