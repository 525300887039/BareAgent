# Design: Confine LSP Workspace Edits

## Boundary and Data Flow

`LanguageServerManager` remains the source of truth for `repository_root`.
The semantic rename handler passes it to a keyword-only `workspace_root`
parameter on `apply_workspace_edit()`. For every URI group, the applier:

1. rejects non-file/virtual URIs;
2. converts the file URI to a platform path;
3. derives its relative path from `workspace_root` (cross-drive failures reject);
4. delegates canonicalization and symlink checks to `core.sandbox.safe_path`;
5. only then reads, edits, and atomically writes the resolved path.

## Contracts and Trade-offs

- `workspace_root` is required rather than silently defaulted, so future callers
  cannot accidentally omit the security boundary.
- Unsafe targets are skipped per file. This preserves current cross-file partial
  application semantics and keeps one bad server entry from blocking safe ones.
- The result continues to key applied files by resolved absolute path.
- Resource operations remain unsupported and are reported alongside other skip
  reasons under a general WorkspaceEdit label.

## Compatibility and Rollback

The function is internal; all repository call sites and direct tests are updated
together. Rollback is the single code/test/spec commit group. No data migration
or configuration change is involved.
