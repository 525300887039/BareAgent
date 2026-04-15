"""OpenTelemetry tracer backend (optional dependency).

Requires the ``opentelemetry`` packages::

    pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
    # or: pip install bareagent[otel]

Activated automatically when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, or when
``[tracing] opentelemetry = true`` appears in the config file.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from src.tracing._api import Span, Tracer


class OTelSpan(Span):
    """Span backed by an OpenTelemetry span."""

    def __init__(self, otel_span: Any) -> None:
        self._span = otel_span

    def set_tag(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, _coerce(value))

    def set_content_tag(self, key: str, value: Any) -> None:
        self._span.set_attribute(f"content.{key}", _coerce(value))

    def set_error(self, error: str) -> None:
        from opentelemetry.trace import StatusCode

        self._span.set_status(StatusCode.ERROR, error)

    def end(self) -> None:
        self._span.end()


class OpenTelemetryTracer(Tracer):
    """Tracer that emits standard OpenTelemetry spans.

    Any OTel-compatible backend (Jaeger, Datadog, Langfuse via OTel, etc.)
    can consume the spans.
    """

    def __init__(self, service_name: str = "bareagent") -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)
        self._provider = provider
        self._tracer = trace.get_tracer("bareagent")
        self._current: OTelSpan | None = None

    def add_exporter(self, exporter: Any, *, batch: bool = True) -> None:
        """Attach a span exporter to the provider."""
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )

        processor = (
            BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
        )
        self._provider.add_span_processor(processor)

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        with self._tracer.start_as_current_span(operation_name) as raw_span:
            span = OTelSpan(raw_span)
            if tags:
                for k, v in tags.items():
                    span.set_tag(k, v)
            prev = self._current
            self._current = span
            try:
                yield span
            finally:
                self._current = prev

    def current_span(self) -> Span | None:
        return self._current

    def flush(self) -> None:
        self._provider.force_flush()

    def shutdown(self) -> None:
        self._provider.shutdown()


def _coerce(value: Any) -> str | int | float | bool:
    """Coerce a value to an OTel-compatible attribute type."""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
