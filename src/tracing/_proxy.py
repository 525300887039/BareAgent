"""Global proxy tracer -- hot-swappable at runtime (Haystack pattern)."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from typing import Any

from src.tracing._api import NullTracer, Span, Tracer


class ProxyTracer(Tracer):
    """Delegates to an inner tracer that can be replaced at runtime."""

    def __init__(self, inner: Tracer | None = None) -> None:
        self._inner: Tracer = inner or NullTracer()
        self._lock = threading.Lock()
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
            yield span

    def current_span(self) -> Span | None:
        return self._inner.current_span()

    def flush(self) -> None:
        self._inner.flush()

    def shutdown(self) -> None:
        self._inner.shutdown()


# Global singleton -- the only import any module needs.
tracer: ProxyTracer = ProxyTracer()


def enable_tracing(provided_tracer: Tracer) -> None:
    """Replace the global tracer backend at runtime."""
    tracer.inner = provided_tracer
