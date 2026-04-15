from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest

from src.tracing._api import NullSpan, NullTracer, Span, Tracer
from src.tracing._proxy import ProxyTracer, enable_tracing, tracer


def test_null_span_methods_are_silent() -> None:
    span = NullSpan()
    span.set_tag("model", "gpt-4")
    span.set_content_tag("input", "hello")
    span.set_error("boom")
    span.end()


def test_null_tracer_yields_null_span() -> None:
    t = NullTracer()
    with t.trace("op") as span:
        assert isinstance(span, NullSpan)
    assert t.current_span() is None


def test_null_tracer_flush_and_shutdown_are_silent() -> None:
    t = NullTracer()
    t.flush()
    t.shutdown()


class _RecordingSpan(Span):
    def __init__(self) -> None:
        self.tags: dict[str, Any] = {}
        self.content_tags: dict[str, Any] = {}
        self.errors: list[str] = []
        self.ended = False

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def set_content_tag(self, key: str, value: Any) -> None:
        self.content_tags[key] = value

    def set_error(self, error: str) -> None:
        self.errors.append(error)

    def end(self) -> None:
        self.ended = True


class _RecordingTracer(Tracer):
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []
        self._current: _RecordingSpan | None = None
        self.flushed = False
        self.shut_down = False

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        span = _RecordingSpan()
        if tags:
            for k, v in tags.items():
                span.set_tag(k, v)
        self.spans.append(span)
        prev = self._current
        self._current = span
        try:
            yield span
        finally:
            span.end()
            self._current = prev

    def current_span(self) -> Span | None:
        return self._current

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.shut_down = True


def test_proxy_tracer_defaults_to_null() -> None:
    proxy = ProxyTracer()
    with proxy.trace("op") as span:
        assert isinstance(span, NullSpan)
    assert proxy.current_span() is None


def test_proxy_tracer_delegates_to_inner() -> None:
    recorder = _RecordingTracer()
    proxy = ProxyTracer(recorder)

    with proxy.trace("llm_call", tags={"model": "test"}) as span:
        span.set_tag("input_tokens", 42)
        assert proxy.current_span() is span

    assert len(recorder.spans) == 1
    assert recorder.spans[0].tags == {"model": "test", "input_tokens": 42}
    assert recorder.spans[0].ended is True
    assert proxy.current_span() is None


def test_proxy_tracer_flush_and_shutdown_delegate() -> None:
    recorder = _RecordingTracer()
    proxy = ProxyTracer(recorder)
    proxy.flush()
    proxy.shutdown()
    assert recorder.flushed is True
    assert recorder.shut_down is True


def test_enable_tracing_swaps_backend(monkeypatch: pytest.MonkeyPatch) -> None:

    original_inner = tracer.inner
    recorder = _RecordingTracer()
    try:
        enable_tracing(recorder)
        assert tracer.inner is recorder

        with tracer.trace("test_op") as span:
            span.set_tag("key", "value")

        assert len(recorder.spans) == 1
    finally:
        tracer.inner = original_inner


def test_proxy_tracer_content_tracing_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BAREAGENT_CONTENT_TRACING_ENABLED", "false")
    proxy = ProxyTracer()
    assert proxy.is_content_tracing_enabled is False

    monkeypatch.setenv("BAREAGENT_CONTENT_TRACING_ENABLED", "true")
    proxy2 = ProxyTracer()
    assert proxy2.is_content_tracing_enabled is True


def test_proxy_tracer_hot_swap_is_thread_safe() -> None:
    import threading

    proxy = ProxyTracer()
    errors: list[Exception] = []

    def _swap() -> None:
        try:
            for _ in range(100):
                proxy.inner = _RecordingTracer()
        except Exception as exc:
            errors.append(exc)

    def _trace() -> None:
        try:
            for _ in range(100):
                with proxy.trace("op"):
                    pass
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_swap),
        threading.Thread(target=_trace),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
