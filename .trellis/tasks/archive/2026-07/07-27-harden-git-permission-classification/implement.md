# Implementation Plan

1. Add failing parameterized tests for destructive Git variants, direct
   `is_dangerous` results, allow-rule ordering, fail-closed behavior, read-only
   variants, branch deletion, and safe near-neighbors.
2. Add private tokenization and Git-invocation parsing helpers in
   `permission/guard.py`.
3. Route shell dangerous and read-only decisions through the shared helpers
   while retaining non-Git patterns and fallback behavior.
4. Run focused permission tests, Ruff format/check, and Pyright.
5. Review all backend spec quality checks, update the error-handling contract,
   and run the full non-manual suite before committing.

Rollback point: revert the helper and its call sites together; the existing
regular expressions remain behaviorally self-contained.
