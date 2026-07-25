# Fix retry backoff overflow

## Goal

Keep exponential retry backoff capped for every accepted retry count so a
transient provider failure is never replaced by an internal arithmetic error.

## Background

- `compute_delay()` currently evaluates `multiplier ** (attempt - 1)` before
  applying `max_delay_sec` (`src/bareagent/core/retry.py:89`).
- With the default finite policy, `compute_delay(1025, ...)` raises
  `OverflowError: (34, 'Result too large')` instead of returning the configured
  30-second cap.
- Retry configuration accepts any positive integer `max_attempts`, so this path
  is reachable through supported configuration rather than only an invalid
  internal policy.

## Requirements

- Prevent overflow in the exponential calculation while preserving the current
  cap and optional full-jitter behavior.
- Add a focused regression test that crosses the floating-point exponent limit.
- Preserve behavior for ordinary attempts and do not change configuration
  defaults or accepted values.

## Acceptance Criteria

- [x] A no-jitter policy with `base_delay_sec=1`, `multiplier=2`, and
  `max_delay_sec=30` returns `30.0` for attempt 1025 or greater without raising.
- [x] Existing retry tests continue to pass, including jitter bounds and normal
  monotonic backoff.
- [x] Ruff, formatting, Pyright, and the full non-manual pytest suite pass.

## Out of Scope

- Redesigning retry configuration limits or the retry loop.
- Changing behavior for manually constructed invalid `RetryPolicy` values.
