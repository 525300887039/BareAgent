from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.debug.interaction_log import InteractionLogger
from src.tracing._api import Span, Tracer
from src.tracing.composite import CompositeTracer
from src.tracing.json_file import JsonFileTracer


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


class _ExplodingSpan(Span):
    def set_tag(self, key: str, value: Any) -> None:
        raise RuntimeError("set_tag exploded")

    def set_content_tag(self, key: str, value: Any) -> None:
        raise RuntimeError("set_content_tag exploded")

    def set_error(self, error: str) -> None:
        raise RuntimeError("set_error exploded")

    def end(self) -> None:
        raise RuntimeError("end exploded")


class _ExplodingTracer(Tracer):
    @contextlib.contextmanager
    def trace(self, operation_name, tags=None, *, parent_span=None):
        yield _ExplodingSpan()

    def current_span(self):
        return None

    def flush(self):
        raise RuntimeError("flush exploded")

    def shutdown(self):
        raise RuntimeError("shutdown exploded")


def test_composite_fans_out_to_all_backends() -> None:
    a = _RecordingTracer()
    b = _RecordingTracer()
    composite = CompositeTracer([a, b])

    with composite.trace("llm_call", tags={"model": "test"}) as span:
        span.set_tag("tokens", 42)
        span.set_content_tag("output", "hello")

    assert len(a.spans) == 1
    assert a.spans[0].tags == {"model": "test", "tokens": 42}
    assert a.spans[0].content_tags == {"output": "hello"}
    assert a.spans[0].ended is True

    assert len(b.spans) == 1
    assert b.spans[0].tags == {"model": "test", "tokens": 42}
    assert b.spans[0].content_tags == {"output": "hello"}
    assert b.spans[0].ended is True


def test_composite_isolates_backend_errors() -> None:
    good = _RecordingTracer()
    bad = _ExplodingTracer()
    composite = CompositeTracer([bad, good])

    with composite.trace("op") as span:
        # These should not raise even though _ExplodingSpan throws
        span.set_tag("key", "value")
        span.set_content_tag("input", "data")
        span.set_error("oops")

    assert len(good.spans) == 1
    assert good.spans[0].tags == {"key": "value"}


def test_composite_flush_and_shutdown_isolate_errors() -> None:
    good = _RecordingTracer()
    bad = _ExplodingTracer()
    composite = CompositeTracer([bad, good])

    composite.flush()
    composite.shutdown()

    assert good.flushed is True
    assert good.shut_down is True


def test_composite_current_span_returns_first_non_none() -> None:
    a = _RecordingTracer()
    b = _RecordingTracer()
    composite = CompositeTracer([a, b])

    assert composite.current_span() is None

    with composite.trace("op"):
        span = composite.current_span()
        assert span is not None


def test_composite_forwards_logger_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: 1.0)
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    jft = JsonFileTracer(logger)
    other = _RecordingTracer()
    composite = CompositeTracer([other, jft])

    seq = composite.log_request(  # type: ignore[attr-defined]
        [{"role": "user", "content": "hi"}], []
    )
    assert seq == 0
    assert "sess-1" in composite.list_sessions()  # type: ignore[attr-defined]


def test_composite_getattr_raises_without_json_file_tracer() -> None:
    other = _RecordingTracer()
    composite = CompositeTracer([other])

    with pytest.raises(AttributeError):
        composite.log_request([], [])  # type: ignore[attr-defined]
