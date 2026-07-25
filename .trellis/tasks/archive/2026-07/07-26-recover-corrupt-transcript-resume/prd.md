# Recover from corrupt transcript resume

## Goal

Keep the interactive REPL alive when `/resume` encounters a corrupt or
unreadable transcript, while leaving the current session state unchanged.

## Background

- `TranscriptManager.load()` currently decodes and `json.loads()` each JSONL
  row without error translation (`src/bareagent/memory/transcript.py:37`).
- The `/resume` handler catches only `FileNotFoundError`
  (`src/bareagent/main.py:4263`).
- Full REPL reproductions confirm invalid JSON escapes as `JSONDecodeError` and
  invalid UTF-8 escapes as `UnicodeDecodeError`, terminating the CLI before the
  next command is read.

## Requirements

- Parse transcript JSONL line by line and turn invalid UTF-8, invalid JSON, or
  non-object rows into a clear `ValueError` containing the transcript path and
  line number.
- Make `/resume` catch expected read/format failures, print an error, and
  continue the input loop without replacing messages or switching runtime
  session state.
- Reject the corrupt snapshot as a whole; do not silently restore a partial
  conversation whose tool/message ordering may be inconsistent.
- Add manager-level parsing regressions and an end-to-end stdio resume
  regression for both invalid JSON and invalid UTF-8.

## Acceptance Criteria

- [x] Invalid JSON and UTF-8 errors identify the transcript and failing line.
- [x] After a failed `/resume`, a following `/exit` is read and the stdio session
  returns 0 instead of raising.
- [x] The output contains a readable error and never reports the corrupt session
  as resumed.
- [x] Valid transcript resume behavior remains unchanged.
- [x] Focused transcript/main tests, Ruff, formatting, Pyright, and the full
  non-manual pytest suite pass.

## Out of Scope

- Automatically repairing, rewriting, or deleting corrupt transcript files.
- Falling back to an older snapshot for the same session ID.
