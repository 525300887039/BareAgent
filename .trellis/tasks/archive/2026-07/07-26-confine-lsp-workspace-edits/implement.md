# Implementation Plan

1. Add required `workspace_root` propagation from the LSP manager-backed handler
   into `apply_workspace_edit()`.
2. Resolve each file URI relative to that root and validate it with `safe_path`
   before any open/write call; record rejected paths in `skipped`.
3. Generalize skipped-entry formatting so boundary failures are not mislabeled.
4. Update direct unit callers and add outside-root, symlink, and mixed handler
   regression coverage for `changes` and `documentChanges`.
5. Run focused LSP tests, affected-file Ruff/format, Pyright, then the full
   repository quality gate. Review the complete diff for cross-layer consistency.

Rollback point: revert the workspace-edit signature, handler propagation, tests,
and associated spec update as one coherent task if compatibility checks fail.
