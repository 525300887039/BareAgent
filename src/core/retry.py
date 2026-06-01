"""Provider-agnostic LLM retry policy: exponential backoff + retryable classification.

A pure module (no LLM / loop / SDK dependencies) so the policy, classifier, and
backoff math are fully unit-testable with injected ``sleep`` / ``rng``. The
agent loop wraps the single provider call site (``_invoke_provider``) with
:func:`run_with_retry`; the SDK clients are constructed with ``max_retries=0``
so this layer owns retries exclusively (no 2xN compound amplification).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Retryable HTTP status codes (aligned with the anthropic/openai SDKs' own
# retryable set + 529 overloaded).
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
# Non-retryable status codes (auth / bad request / model-not-found, etc.) —
# raise immediately so a config error is never masked by retries.
_NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 413, 422})
# Retryable connection / timeout / server classes recognized by class name
# (no SDK import, so this stays cross-provider).
_RETRYABLE_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "APIConnectionTimeoutError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
        "ConnectionError",
        "Timeout",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
    }
)


@dataclass(slots=True)
class RetryPolicy:
    enabled: bool = True
    max_attempts: int = 3  # total attempts (incl. first), <=1 disables retries
    base_delay_sec: float = 1.0
    max_delay_sec: float = 30.0
    multiplier: float = 2.0
    jitter: bool = True


def is_retryable(exc: BaseException) -> bool:
    """Provider-agnostic retryable check.

    Looks at ``status_code`` first, then the class name; unknown -> not retryable.
    """
    # Non-Exception (KeyboardInterrupt / SystemExit) is never retried.
    if not isinstance(exc, Exception):
        return False
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status in _NON_RETRYABLE_STATUS:
            return False
        if status in _RETRYABLE_STATUS:
            return True
        # Any other 5xx is retryable; everything else is explicitly not.
        return 500 <= status < 600
    # No status code: match connection / timeout classes by name (including MRO).
    for klass in type(exc).__mro__:
        if klass.__name__ in _RETRYABLE_NAMES:
            return True
    return False


def compute_delay(
    attempt: int,
    policy: RetryPolicy,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Exponential backoff + cap + optional full jitter.

    ``attempt`` starts at 1 (the wait before the first retry).
    """
    raw = policy.base_delay_sec * (policy.multiplier ** max(0, attempt - 1))
    capped = min(policy.max_delay_sec, raw)
    if policy.jitter:
        return rng(0.0, capped)
    return capped


def run_with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[BaseException, int, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run ``fn``, backing off and retrying retryable exceptions per ``policy``.

    - Non-retryable exceptions / non-Exception (KeyboardInterrupt, etc.) re-raise immediately.
    - After exhausting ``max_attempts``, re-raises the **last** original exception
      (preserving ``__cause__`` is the caller's responsibility).
    - ``on_retry(exc, next_attempt, delay)`` is invoked before each sleep (observability).
    """
    if not policy.enabled or policy.max_attempts <= 1:
        return fn()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - must propagate non-Exception
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            delay = compute_delay(attempt, policy, rng=rng)
            if on_retry is not None:
                on_retry(exc, attempt + 1, delay)
            logger.warning(
                "LLM call failed (%s), retrying in %.2fs (attempt %d/%d)",
                type(exc).__name__,
                delay,
                attempt + 1,
                policy.max_attempts,
            )
            sleep(delay)
