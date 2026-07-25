# Fix confirmed maintenance regressions

## Goal

Fix four independently reproduced edge-case regressions found during the daily
repository audit without changing normal-path behavior.

## Requirements

- AUTO permission mode must require confirmation for destructive Git variants
  and recursive forced PowerShell deletion, regardless of command casing.
- Repeating scheduler intervals and retry backoff configuration must reject or
  normalize non-finite floats before they reach `threading.Timer` or `sleep`.
- A corrupt session-lineage sidecar, including invalid UTF-8 or non-finite
  numeric fields, must fail open without hiding real transcripts.
- Clipboard file-list images must retain an extension matching their source
  bytes; raw clipboard images must continue to be saved as PNG.
- Existing safe-command, valid retry, valid lineage, and PNG clipboard behavior
  must remain unchanged.

## Acceptance Criteria

- [x] `Remove-Item -Recurse -Force`, case variants, `git push -f`, and
      `git clean -fdx` require confirmation in AUTO mode.
- [x] `Scheduler.add()` rejects NaN and both infinities without arming a timer.
- [x] Retry config replaces non-finite delays/multipliers and always returns
      `max_delay_sec >= base_delay_sec`.
- [x] Invalid UTF-8 lineage returns `{}` and malformed numeric records are
      skipped while valid records survive.
- [x] A copied JPEG clipboard file is stored with a JPEG extension while the
      existing raw-image PNG path is unchanged.
- [x] Targeted tests and the complete repository quality gate pass.

## Notes

- All four defects were reproduced read-only on clean `main`; existing targeted
  suites passed, confirming they are uncovered boundary cases rather than
  pre-existing red tests.
- Verification: the five directly affected test modules pass 120 tests; the
  default suite passes 1442 tests with 47 deselected; the socket suite passes
  11 tests with 1478 deselected. Ruff lint and format checks pass, and Pyright
  reports 0 errors (7 existing optional-dependency warnings).
