# Design

## Boundaries

- Permission detection remains centralized in `PermissionGuard`; handlers do
  not add safety checks.
- Timing validation happens at configuration/scheduler entry boundaries, before
  values reach retry sleep or timer threads.
- Session-lineage loading remains a fail-open sidecar read; no repair write is
  attempted.
- Clipboard file-list handling copies bytes unchanged and therefore preserves
  the source suffix. Raw PIL images continue using the caller-provided PNG name.

## Changes

1. Compile shell danger patterns case-insensitively and add narrow patterns for
   forced recursive `Remove-Item`, short-form force push, and forced Git clean.
2. Require finite scheduler intervals. Use `math.isfinite` for retry float
   fields, then re-establish the max/base invariant after default fallback.
3. Treat Unicode decode errors as unreadable lineage and treat integer
   overflow as an invalid individual lineage record.
4. Replace the generated filename suffix with the copied source image suffix
   in the clipboard file-list branch only.

## Compatibility and rollback

Normal valid inputs are unchanged. Permission changes are conservative prompts,
not execution failures. Each area has a focused regression test and can be
reverted independently if a compatibility issue appears.
