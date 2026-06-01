from __future__ import annotations

from copy import deepcopy

import pytest

from src.core.loop import LLMCallError, agent_loop
from src.core.retry import (
    RetryPolicy,
    compute_delay,
    is_retryable,
    run_with_retry,
)
from src.provider.base import BaseLLMProvider, LLMResponse

# --- Fake exceptions ------------------------------------------------------


class _StatusError(Exception):
    """Fake SDK error carrying a ``status_code`` attribute."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    """Name matches the retryable connection-class whitelist."""


class APITimeoutError(Exception):
    """Name matches the retryable timeout-class whitelist."""


class _MysteryError(Exception):
    """Unknown error: no status_code, name not in whitelist."""


class _FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


# --- is_retryable ---------------------------------------------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
def test_is_retryable_retryable_status(status: int) -> None:
    assert is_retryable(_StatusError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_is_retryable_non_retryable_status(status: int) -> None:
    assert is_retryable(_StatusError(status)) is False


def test_is_retryable_unknown_5xx_is_retryable() -> None:
    assert is_retryable(_StatusError(599)) is True


def test_is_retryable_connection_and_timeout_class_names() -> None:
    assert is_retryable(APIConnectionError("boom")) is True
    assert is_retryable(APITimeoutError("boom")) is True
    assert is_retryable(TimeoutError("boom")) is True


def test_is_retryable_status_attr_alias() -> None:
    exc = Exception("e")
    exc.status = 503  # type: ignore[attr-defined]
    assert is_retryable(exc) is True


def test_is_retryable_unknown_exception_is_not_retryable() -> None:
    assert is_retryable(_MysteryError("???")) is False
    assert is_retryable(ValueError("bad")) is False


def test_is_retryable_non_exception_never_retried() -> None:
    assert is_retryable(KeyboardInterrupt()) is False
    assert is_retryable(SystemExit()) is False


# --- compute_delay --------------------------------------------------------


def test_compute_delay_no_jitter_is_monotonic_and_capped() -> None:
    policy = RetryPolicy(base_delay_sec=1.0, max_delay_sec=30.0, multiplier=2.0, jitter=False)
    delays = [compute_delay(attempt, policy) for attempt in range(1, 8)]
    # 1, 2, 4, 8, 16, then capped at 30.
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays[3] == 8.0
    assert delays[4] == 16.0
    # Monotonically non-decreasing.
    for earlier, later in zip(delays, delays[1:], strict=False):
        assert later >= earlier
    # Never exceeds the cap.
    assert all(d <= 30.0 for d in delays)
    assert delays[-1] == 30.0


def test_compute_delay_large_attempt_stays_capped() -> None:
    policy = RetryPolicy(base_delay_sec=1.0, max_delay_sec=30.0, multiplier=2.0, jitter=False)
    assert compute_delay(50, policy) == 30.0


def test_compute_delay_jitter_passes_capped_bounds_to_rng() -> None:
    policy = RetryPolicy(base_delay_sec=1.0, max_delay_sec=10.0, multiplier=2.0, jitter=True)
    seen: list[tuple[float, float]] = []

    def _record(low: float, high: float) -> float:
        seen.append((low, high))
        return low  # any value in [low, high]

    # attempt=2 -> raw 2.0, capped 2.0; jitter draws from [0, 2.0].
    result = compute_delay(2, policy, rng=_record)
    assert seen == [(0.0, 2.0)]
    assert 0.0 <= result <= 2.0

    # attempt=10 -> raw huge, capped at 10.0; jitter draws from [0, 10.0].
    seen.clear()
    compute_delay(10, policy, rng=_record)
    assert seen == [(0.0, 10.0)]


# --- run_with_retry -------------------------------------------------------


def test_run_with_retry_first_success_no_sleep() -> None:
    sleep = _FakeSleep()
    calls = {"n": 0}

    def _fn() -> str:
        calls["n"] += 1
        return "ok"

    policy = RetryPolicy(max_attempts=3, jitter=False)
    assert run_with_retry(_fn, policy, sleep=sleep) == "ok"
    assert calls["n"] == 1
    assert sleep.delays == []


def test_run_with_retry_succeeds_after_retries() -> None:
    sleep = _FakeSleep()
    attempts = {"n": 0}

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _StatusError(429)
        return "done"

    policy = RetryPolicy(
        max_attempts=3, base_delay_sec=1.0, max_delay_sec=30.0, multiplier=2.0, jitter=False
    )
    result = run_with_retry(_fn, policy, sleep=sleep)
    assert result == "done"
    assert attempts["n"] == 3
    # Two sleeps before the third (successful) attempt: delays 1.0 then 2.0.
    assert sleep.delays == [1.0, 2.0]


def test_run_with_retry_exhausts_and_raises_last_exception() -> None:
    sleep = _FakeSleep()
    raised: list[_StatusError] = []

    def _fn() -> str:
        exc = _StatusError(503)
        raised.append(exc)
        raise exc

    policy = RetryPolicy(max_attempts=3, jitter=False)
    with pytest.raises(_StatusError) as info:
        run_with_retry(_fn, policy, sleep=sleep)
    # Tried max_attempts times; the surfaced exception is the last one raised.
    assert len(raised) == 3
    assert info.value is raised[-1]
    # Slept between attempts only (N-1 times).
    assert len(sleep.delays) == 2


def test_run_with_retry_non_retryable_raises_immediately() -> None:
    sleep = _FakeSleep()
    calls = {"n": 0}

    def _fn() -> str:
        calls["n"] += 1
        raise _StatusError(401)

    policy = RetryPolicy(max_attempts=5, jitter=False)
    with pytest.raises(_StatusError):
        run_with_retry(_fn, policy, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.delays == []


def test_run_with_retry_keyboard_interrupt_propagates() -> None:
    sleep = _FakeSleep()
    calls = {"n": 0}

    def _fn() -> str:
        calls["n"] += 1
        raise KeyboardInterrupt()

    policy = RetryPolicy(max_attempts=5, jitter=False)
    with pytest.raises(KeyboardInterrupt):
        run_with_retry(_fn, policy, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.delays == []


def test_run_with_retry_on_retry_callback_args() -> None:
    sleep = _FakeSleep()
    events: list[tuple[str, int, float]] = []
    attempts = {"n": 0}

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _StatusError(500)
        return "ok"

    def _on_retry(exc: BaseException, next_attempt: int, delay: float) -> None:
        events.append((type(exc).__name__, next_attempt, delay))

    policy = RetryPolicy(
        max_attempts=3, base_delay_sec=1.0, max_delay_sec=30.0, multiplier=2.0, jitter=False
    )
    run_with_retry(_fn, policy, on_retry=_on_retry, sleep=sleep)
    assert events == [
        ("_StatusError", 2, 1.0),
        ("_StatusError", 3, 2.0),
    ]


def test_run_with_retry_disabled_calls_fn_once() -> None:
    sleep = _FakeSleep()
    calls = {"n": 0}

    def _fn() -> str:
        calls["n"] += 1
        raise _StatusError(429)

    policy = RetryPolicy(enabled=False, max_attempts=5)
    with pytest.raises(_StatusError):
        run_with_retry(_fn, policy, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.delays == []


def test_run_with_retry_max_attempts_one_calls_fn_once() -> None:
    sleep = _FakeSleep()
    calls = {"n": 0}

    def _fn() -> str:
        calls["n"] += 1
        raise _StatusError(429)

    policy = RetryPolicy(max_attempts=1)
    with pytest.raises(_StatusError):
        run_with_retry(_fn, policy, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.delays == []


# --- agent_loop integration ----------------------------------------------


class _FlakyProvider(BaseLLMProvider):
    """Raises ``error`` for the first ``fail_times`` create() calls, then succeeds."""

    def __init__(self, error: Exception, fail_times: int, response: LLMResponse) -> None:
        self._error = error
        self._fail_times = fail_times
        self._response = response
        self.create_calls = 0
        self.model = "fake-model"

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = (deepcopy(messages), deepcopy(tools), kwargs)
        self.create_calls += 1
        if self.create_calls <= self._fail_times:
            raise self._error
        return self._response

    def create_stream(self, messages, tools, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


def _text_response(text: str = "hello") -> LLMResponse:
    return LLMResponse(text=text, stop_reason="end_turn", input_tokens=1, output_tokens=1)


def test_agent_loop_retries_transient_then_succeeds() -> None:
    provider = _FlakyProvider(_StatusError(429), fail_times=2, response=_text_response("ok"))
    policy = RetryPolicy(max_attempts=3, base_delay_sec=0.0, max_delay_sec=0.0, jitter=False)
    messages: list[dict] = [{"role": "user", "content": "hi"}]

    # base_delay/max_delay = 0 keeps sleep instantaneous; no wall-clock dependency.
    result = agent_loop(
        provider=provider,
        messages=messages,
        tools=[],
        handlers={},
        retry_policy=policy,
    )
    assert result == "ok"
    assert provider.create_calls == 3


def test_agent_loop_non_retryable_raises_llm_call_error() -> None:
    provider = _FlakyProvider(_StatusError(401), fail_times=5, response=_text_response())
    policy = RetryPolicy(max_attempts=3, base_delay_sec=0.0, max_delay_sec=0.0, jitter=False)
    messages: list[dict] = [{"role": "user", "content": "hi"}]

    with pytest.raises(LLMCallError):
        agent_loop(
            provider=provider,
            messages=messages,
            tools=[],
            handlers={},
            retry_policy=policy,
        )
    # Non-retryable: only the first attempt ran.
    assert provider.create_calls == 1


def test_agent_loop_no_policy_is_single_call() -> None:
    provider = _FlakyProvider(_StatusError(429), fail_times=1, response=_text_response())
    messages: list[dict] = [{"role": "user", "content": "hi"}]

    with pytest.raises(LLMCallError):
        agent_loop(
            provider=provider,
            messages=messages,
            tools=[],
            handlers={},
        )
    assert provider.create_calls == 1
