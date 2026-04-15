"""Fan-out tracer that delegates to N backends."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from src.tracing._api import Span, Tracer


class CompositeSpan(Span):
    """Span that fans out to multiple backend spans."""

    def __init__(self, spans: list[Span]) -> None:
        self._spans = spans

    def set_tag(self, key: str, value: Any) -> None:
        for span in self._spans:
            try:
                span.set_tag(key, value)
            except Exception:
                pass

    def set_content_tag(self, key: str, value: Any) -> None:
        for span in self._spans:
            try:
                span.set_content_tag(key, value)
            except Exception:
                pass

    def set_error(self, error: str) -> None:
        for span in self._spans:
            try:
                span.set_error(error)
            except Exception:
                pass

    def end(self) -> None:
        for span in self._spans:
            try:
                span.end()
            except Exception:
                pass


class CompositeTracer(Tracer):
    """Tracer that fans out to N backends simultaneously."""

    def __init__(self, tracers: list[Tracer]) -> None:
        self._tracers = list(tracers)

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        spans: list[Span] = []
        exits: list[contextlib.AbstractContextManager[Span]] = []
        try:
            for tracer in self._tracers:
                cm = tracer.trace(operation_name, tags, parent_span=parent_span)
                span = cm.__enter__()
                spans.append(span)
                exits.append(cm)
            yield CompositeSpan(spans)
        finally:
            for cm in reversed(exits):
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass

    def current_span(self) -> Span | None:
        for tracer in self._tracers:
            span = tracer.current_span()
            if span is not None:
                return span
        return None

    def flush(self) -> None:
        for tracer in self._tracers:
            try:
                tracer.flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        for tracer in self._tracers:
            try:
                tracer.shutdown()
            except Exception:
                pass

    # ---- Delegate InteractionLogger query methods to first JsonFileTracer ----

    def _json_file_tracer(self) -> Any:
        from src.tracing.json_file import JsonFileTracer

        for tracer in self._tracers:
            if isinstance(tracer, JsonFileTracer):
                return tracer
        return None

    def __getattr__(self, name: str) -> Any:
        """Forward InteractionLogger methods to the first JsonFileTracer."""
        jft = self._json_file_tracer()
        if jft is not None:
            attr = getattr(jft, name, None)
            if attr is not None:
                return attr
        raise AttributeError(name)
