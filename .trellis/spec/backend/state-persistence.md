# State & Persistence

> How BareAgent stores state. There is no database — everything is local files.

BareAgent has no SQL database, no ORM, no migrations. It is a single-process terminal app that writes state to local files under the workspace root. All persistence rules below trade query power for **append-only durability**, **zero external dependencies**, and **trivial inspectability** (`cat` / `jq`).

If you find yourself reaching for SQLite or a key-value store, stop and reconsider — the existing files are sufficient for the agent's working-set sizes.

---

## Storage formats at a glance

| Concern | Location | Format | Module |
|---|---|---|---|
| Inter-agent messages | `.mailbox/<session_id>/<agent>.jsonl` | JSONL, append-only | `src/team/mailbox.py` |
| Persistent tasks (with deps) | `.tasks.json` | Single JSON object, atomic write | `src/planning/tasks.py` |
| Session transcripts | `.transcripts/<session_id>_<timestamp>.jsonl` | JSONL snapshot | `src/memory/transcript.py` |
| Session fork lineage | `.transcripts/.tree.json` | Single JSON object, atomic write | `src/memory/session_tree.py` |
| LLM request/response logs | `.logs/<session_id>/<seq>_request.json` + `<seq>_response.json` | JSON, sequence-numbered | `src/debug/interaction_log.py` |
| Teammate roster | `.team.json` | JSON, atomic write | `src/team/manager.py` |
| User config (defaults) | `config.toml` | TOML, checked in | `src/main.py` |
| Local overrides | `config.local.toml` | TOML, **git-ignored** | `src/main.py` |
| Prompt history | `.bareagent_history` | prompt-toolkit format | `src/ui/prompt.py` |

**Rule**: state directories at the workspace root start with `.` (hidden). New persistence sites must follow this convention so users can `.gitignore` them in one line.

---

## Append-only JSONL for event streams

Use JSONL whenever the data is a **stream of events** that grows over time and is read in order.

**Why**: appending a line is atomic on POSIX (and good enough on Windows for our concurrency level); readers can stream without loading the whole file; corrupt lines can be skipped without losing the rest.

Canonical example — `MessageBus._append` in `src/team/mailbox.py`:

```python
def _append(self, agent_name: str, msg: Message) -> None:
    mailbox_path = self.ensure_mailbox(agent_name)
    line = json.dumps(msg.to_dict(), ensure_ascii=False)
    with self._lock_for(agent_name):
        with mailbox_path.open("a", encoding="utf-8") as file:
            file.write(line)
            file.write("\n")
```

Notes that any new JSONL writer must replicate:

- One JSON object per line, `ensure_ascii=False` (we want raw UTF-8, not `\uXXXX`).
- Per-file `threading.Lock` to serialize writes; readers do their own validation.
- Validate-on-read with a clear error message that includes file + line number (see `MessageBus.receive` in `mailbox.py`).

---

## Scenario: Resume a transcript snapshot

### 1. Scope / Trigger

This contract applies to `.transcripts/*.jsonl` reads used by `/resume`.
Unlike an independent mailbox event stream, a transcript is an ordered
conversation snapshot whose assistant/tool-result relationships must remain
intact; never restore only the rows that happened to parse.

### 2. Signatures

- `TranscriptManager.load(session_id) -> list[dict[str, Any]]` validates the
  selected snapshot and raises `ValueError` for format corruption.
- `TranscriptManager.resume(session_id | None)` preserves that contract after
  selecting the requested or latest snapshot.
- The `/resume` REPL branch catches expected `OSError` and `ValueError` before
  mutating live session state.

### 3. Contracts

- Read in binary mode and decode each physical line as UTF-8 so both decoding
  and JSON errors can report the exact file and line number.
- Every non-blank row must decode to one JSON object; arrays and scalar JSON are
  invalid transcript entries.
- Accumulate into a new list and return it only after the entire file validates.
- On failure, do not replace `messages`, reset tokens, switch runtime/mailbox
  session, clear registries, or rewrite/delete the corrupt file.
- Print the failure and continue the REPL input loop.

### 4. Validation & Error Matrix

- Session/snapshot missing -> `FileNotFoundError`, displayed by `/resume`.
- File open/read failure -> `OSError`, displayed by `/resume`.
- Invalid UTF-8 -> `ValueError` with path and physical line.
- Invalid JSON -> `ValueError` with path, physical line, and parser reason.
- Valid JSON that is not an object -> `ValueError` with path and physical line.
- All rows valid -> atomically replace the live message list, then switch the
  other runtime session components.

### 5. Good / Base / Bad Cases

- Good: a valid snapshot restores every message and switches runtime state.
- Base: a corrupt snapshot prints an error; the next command (including
  `/exit`) still runs in the existing session.
- Bad: letting `JSONDecodeError` or `UnicodeDecodeError` escape the command loop,
  or skipping one row and restoring a structurally incomplete conversation.

### 6. Tests Required

Manager tests must cover invalid JSON, invalid UTF-8, and non-object rows with
path + line assertions. Stdio integration must parameterize valid, invalid-JSON,
and invalid-UTF-8 `/resume` inputs followed by `/exit`, asserting return code 0
and no false `Resumed session` message on corruption.

### 7. Wrong vs Correct

```python
# Wrong: decoding/parsing exceptions escape and partial acceptance is tempting.
return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]

# Correct: validate a fresh list completely before the caller swaps live state.
messages = []
for line_number, raw_line in enumerate(path.open("rb"), start=1):
    message = json.loads(raw_line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError(f"Invalid transcript entry at line {line_number}")
    messages.append(message)
return messages
```

---

## Mutable structured state: write whole file atomically

For state that is read-modify-write (task graphs, teammate rosters), use `atomic_write_json` from `src/core/fileutil.py`:

```python
def atomic_write_json(file_path: Path, payload: Any) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(file_path))
    except BaseException:
        ...
```

**Why**: `os.replace` is atomic on every supported platform — a reader either sees the old file or the new one, never a half-written one. **Never** write JSON directly with `path.write_text(json.dumps(...))` for persistent state.

Example caller — `TaskManager._save` in `src/planning/tasks.py`:

```python
def _save(self) -> None:
    payload = {"tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()}}
    atomic_write_json(self.task_file, payload)
```

`TaskManager` also holds a `threading.RLock` for the whole load/mutate/save cycle. Any new on-disk structured state must do the same.

---

## Session lineage rendering fails open

The transcript list is authoritative for which sessions exist; the optional
`.transcripts/.tree.json` sidecar only contributes parent/child display edges. A corrupt
sidecar must never make a real transcript disappear.

`memory/session_tree.py::render_tree` therefore walks normal roots first, then walks every
session that remains unvisited in the original transcript-list order. Its cycle guard runs
before emitting a node, so pure cycles and self-cycles terminate without rendering any
session twice. Keep this order when changing the renderer: checking `visited` after output
prevents infinite recursion but still duplicates the node reached by a cycle's back edge.

Do not rewrite or "repair" the sidecar during rendering. Rendering is a fail-open read path;
persisted lineage changes continue to go through the atomic writer.

Loading follows the same rule at both corruption scopes: unreadable bytes,
invalid UTF-8, invalid JSON, or a non-object document discard only the optional
sidecar and return an empty lineage map; a malformed individual record is
skipped while valid sibling records survive. Numeric coercion must also treat
non-finite/overflowing JSON values as a bad record rather than allowing the
sidecar to break `/tree` or `/fork`.

---

## Session IDs are timestamp + random suffix

Session IDs are generated by `_generate_session_id()` in `src/main.py`:

```
20260527-143012-123456-xY7kQp
└─── strftime("%Y%m%d-%H%M%S-%f") ───┘ + "-" + generate_random_id(6)
```

**Why**: lexicographic sort = chronological sort (good for `ls`, transcript listing); the random suffix prevents collisions when a user runs `/new` twice in the same microsecond. Validated by `InteractionLogger._validate_session_id` to forbid path separators and absolute paths.

Whenever a feature opens a session-scoped file (`.logs/<id>/`, `.mailbox/<id>/`, `.transcripts/<id>_*.jsonl`), use the existing session ID — never invent your own.

---

## Sequence-numbered files for ordered records

`InteractionLogger` writes one file per LLM round-trip under `.logs/<session_id>/`:

```
000_request.json
000_response.json
001_request.json
001_response.json
...
```

The next sequence number is computed by `_discover_next_seq()` — it scans existing files and takes `max(seq) + 1`. This makes the directory **resumable**: if the process crashes mid-session, the next start picks up the right number without external state.

Use this pattern when you need ordered, individually-inspectable records. Use JSONL when readers will scan the whole stream in order. Don't mix them in the same directory.

---

## Config layering (precedence: low → high)

Config is resolved in this order — later sources override earlier ones:

1. `config.toml` (project defaults, checked in)
2. `config.local.toml` (developer overrides, **git-ignored**)
3. Environment variables (`BAREAGENT_*`)
4. CLI arguments (`--provider`, `--model`, `--config`)

Implemented in `src/main.py`:`_read_config_file` (deep merges base + `.local`) and the `_resolve_string` / `_resolve_bool` / `_resolve_int` helpers (env > file).

**Rule for new config keys**:

- Add the default to `config.toml`.
- Add a typed field to the matching dataclass (`ProviderConfig`, `UIConfig`, `DebugConfig`, etc.) in `src/main.py`.
- Wire an env var (`BAREAGENT_<UPPER_SNAKE>`) through the resolver helpers.
- Document the env var in `CLAUDE.md` and `README.md` config tables.

Never read `config.toml` directly from a package other than `main.py`. Pass typed config objects down.

---

## General principles

- **Append-only beats mutate-in-place.** Events go into JSONL; state goes into atomically-replaced JSON. Never edit a file in place with random-access seeks.
- **Local files beat external dependencies.** A new persistence requirement is a new file under `.<dirname>/`, not a new service.
- **Workspace-relative paths only.** Use `src/core/sandbox.py::safe_path` for any user-controlled path; never resolve `~` or absolute paths from tool input.
- **Crash safety = atomic rename + sequence discovery.** Both `atomic_write_json` and `InteractionLogger._discover_next_seq` survive a hard kill without corrupting state.
- **State directories are git-ignored.** Anything generated at runtime (`.mailbox/`, `.transcripts/`, `.logs/`, `.tasks.json`, `.team.json`, `.bareagent_history`) is excluded from version control.
