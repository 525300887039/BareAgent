# Implementation Plan

1. Add failing parameterized regressions for compound safe-prefix commands
   (separators, newlines, redirection, and substitution), quoted/full-path
   Windows wrappers, and Unix quote concatenation. Include direct
   `requires_confirm` and `is_dangerous` assertions plus safe neighbors.
2. Add side-effect-free helpers for whole-command control-syntax detection,
   opaque shell wrapper recognition, and POSIX quote-normalized tokenization.
3. Route dangerous Git discovery and DEFAULT auto-safe decisions through those
   helpers without changing permission-mode ordering.
4. Run focused permission tests, Ruff format/check, and Pyright; inspect the
   diff for false positives and duplicated parsing.
5. Update the permission error-handling specification and run the full
   non-manual test suite before committing.

Rollback point: revert the helper and call-site changes as one unit; no storage
format or public API changes are involved.
