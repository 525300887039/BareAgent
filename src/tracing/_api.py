"""Tracing abstractions: Span and Tracer ABCs."""

from __future__ import annotations

import abc
import contextlib
from collections.abc import Iterator
from typing import Any


class Span(abc.ABC):
    """A single instrumented operation."""

    @abc.abstractmethod
    def set_tag(self, key: str, value: Any) -> None:
        """Attach metadata (model name, tool name, etc.)."""

    @abc.abstractmethod
    def set_content_tag(self, key: str, value: Any) -> None:
        """Attach content-sensitive data (messages, outputs).

        Backends may skip these if content tracing is disabled.
        """

    @abc.abstractmethod
    def set_error(self, error: str) -> None:
        """Mark the span as failed."""

    @abc.abstractmethod
    def end(self) -> None:
        """Finalize the span."""


class Tracer(abc.ABC):
    """Interface for instrumenting code with spans."""

    @abc.abstractmethod
    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        """Create a span for the given operation."""

    @abc.abstractmethod
    def current_span(self) -> Span | None:
        """Return the currently active span, if any."""

    def flush(self) -> None:
        """Flush pending data to the backend. No-op by default."""

    def shutdown(self) -> None:
        """Release resources. No-op by default."""


class NullSpan(Span):
    """Zero-overhead no-op span."""

    def set_tag(self, key: str, value: Any) -> None:
        pass

    def set_content_tag(self, key: str, value: Any) -> None:
        pass

    def set_error(self, error: str) -> None:
        pass

    def end(self) -> None:
        pass


class NullTracer(Tracer):
    """Zero-overhead no-op tracer."""

    _NULL_SPAN = NullSpan()

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        yield self._NULL_SPAN

    def current_span(self) -> Span | None:
        return None
