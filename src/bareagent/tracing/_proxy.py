"""Global proxy tracer -- hot-swappable at runtime (Haystack pattern)."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from typing import Any

from bareagent.tracing._api import NullSpan, NullTracer, Span, Tracer


class ProxyTracer(Tracer):
    """Delegates to an inner tracer that can be replaced at runtime."""

    def __init__(self, inner: Tracer | None = None) -> None:
        self._inner: Tracer = inner or NullTracer()
        self._lock = threading.Lock()
        self._local = threading.local()
        self.is_content_tracing_enabled: bool = os.getenv(
            "BAREAGENT_CONTENT_TRACING_ENABLED", "true"
        ).lower() in {"1", "true", "yes", "on"}

    @property
    def inner(self) -> Tracer:
        return self._inner

    @inner.setter
    def inner(self, tracer: Tracer) -> None:
        with self._lock:
            self._inner = tracer

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        with self._inner.trace(operation_name, tags, parent_span=parent_span) as span:
            if isinstance(span, NullSpan):
                yield span
                return
            controlled_span = _ContentControlledSpan(span, self)
            previous = getattr(self._local, "current_span", None)
            self._local.current_span = controlled_span
            try:
                yield controlled_span
            finally:
                self._local.current_span = previous

    def current_span(self) -> Span | None:
        span = getattr(self._local, "current_span", None)
        if isinstance(span, Span):
            return span
        return self._inner.current_span()

    def flush(self) -> None:
        self._inner.flush()

    def shutdown(self) -> None:
        self._inner.shutdown()

    def set_session_id(self, session_id: str) -> None:
        """Propagate a session switch to backends that support sessions."""
        setter = getattr(self._inner, "set_session_id", None)
        if callable(setter):
            setter(session_id)
            return
        if hasattr(self._inner, "session_id"):
            inner: Any = self._inner
            inner.session_id = session_id


class _ContentControlledSpan(Span):
    def __init__(self, inner: Span, proxy: ProxyTracer) -> None:
        self._inner = inner
        self._proxy = proxy

    def set_tag(self, key: str, value: Any) -> None:
        self._inner.set_tag(key, value)

    def set_content_tag(self, key: str, value: Any) -> None:
        if self._proxy.is_content_tracing_enabled:
            self._inner.set_content_tag(key, value)

    def set_error(self, error: str) -> None:
        self._inner.set_error(error)

    def end(self) -> None:
        self._inner.end()


# Global singleton -- the only import any module needs.
tracer: ProxyTracer = ProxyTracer()


def enable_tracing(provided_tracer: Tracer) -> None:
    """Replace the global tracer backend at runtime."""
    tracer.inner = provided_tracer
