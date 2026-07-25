# Confine LSP workspace edits

## Goal

Confine every LSP-provided `WorkspaceEdit` target to the configured repository
root so semantic rename cannot overwrite files outside the active workspace.

## Background

- `apply_workspace_edit()` converts each server-provided URI to an absolute
  path and reads/writes it directly (`src/bareagent/lsp/workspace_edit.py:253`).
- The semantic rename handler calls it without the repository root
  (`src/bareagent/lsp/tools.py:381`), although the manager exposes that root.
- An in-memory reproduction confirmed that a `file:` URI outside the repository
  is accepted, written, and reported as applied.

## Requirements

- Make the repository root an explicit input to workspace-edit application and
  pass `LanguageServerManager.repository_root` from the production handler.
- Validate every decoded file URI before any file read or write, reusing the
  project's workspace sandbox rules so parent, cross-drive, and symlink escapes
  are rejected.
- Preserve current partial-application behavior: valid in-workspace text edits
  still apply while unsafe or unsupported entries are recorded as skipped.
- Cover both `changes` and `documentChanges` WorkspaceEdit forms.
- Keep skip/error summaries accurate for unsafe entries; do not describe every
  skipped entry as a resource operation.

## Acceptance Criteria

- [x] An in-workspace edit applies and is reported normally.
- [x] An outside-workspace `file:` URI is never opened or written and appears in
  `result.skipped` with a clear workspace-boundary reason.
- [x] A symlink inside the workspace that targets an outside file is rejected.
- [x] Handler-level tests prove mixed safe/unsafe edits update only safe files
  for both WorkspaceEdit shapes.
- [x] Focused LSP tests, Ruff, formatting, Pyright, and the full non-manual
  pytest suite pass.

## Out of Scope

- Supporting non-file or LSP resource create/rename/delete operations.
- Allowing language servers to edit explicitly configured external roots.
