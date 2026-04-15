"""Configure tracing from config + environment (Haystack auto-detect pattern)."""

from __future__ import annotations

import os
from typing import Any

from src.tracing._api import Tracer
from src.tracing._proxy import enable_tracing


def configure_tracing(
    tracing_config: Any,
    *,
    session_id: str = "default",
    interaction_logger: Any = None,
) -> None:
    """Read ``[tracing]`` config and wire up the global tracer.

    Backends are activated by configuration or environment variables:

    - **JsonFile**: always active when *interaction_logger* is provided
      (backward compat with ``/log`` and the debug web viewer).
    - **Langfuse**: ``LANGFUSE_PUBLIC_KEY`` env var, or
      ``[tracing] langfuse = true``.
    - **OpenTelemetry**: ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, or
      ``[tracing] opentelemetry = true``.

    When multiple backends are active a :class:`CompositeTracer` fans
    out to all of them.
    """

    backends: list[Tracer] = []

    # 1) JsonFile backend (always on if interaction_logger provided)
    if interaction_logger is not None:
        from src.tracing.json_file import JsonFileTracer

        backends.append(JsonFileTracer(interaction_logger))

    # 2) Langfuse (config or env-var driven)
    if _langfuse_enabled(tracing_config):
        try:
            from src.tracing.langfuse import LangfuseTracer

            backends.append(LangfuseTracer(session_id=session_id))
        except ImportError:
            pass  # langfuse not installed

    # 3) OpenTelemetry (auto-detect: if OTEL_EXPORTER_OTLP_ENDPOINT is set)
    if _otel_enabled(tracing_config):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            from src.tracing.otel import OpenTelemetryTracer

            otel = OpenTelemetryTracer()
            otel.add_exporter(OTLPSpanExporter())
            backends.append(otel)
        except ImportError:
            pass  # opentelemetry not installed

    if not backends:
        return  # NullTracer remains active

    if len(backends) == 1:
        enable_tracing(backends[0])
    else:
        from src.tracing.composite import CompositeTracer

        enable_tracing(CompositeTracer(backends))


def _langfuse_enabled(config: Any) -> bool:
    if os.getenv("LANGFUSE_PUBLIC_KEY"):
        return True
    return bool(getattr(config, "langfuse", False))


def _otel_enabled(config: Any) -> bool:
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return True
    return bool(getattr(config, "opentelemetry", False))
