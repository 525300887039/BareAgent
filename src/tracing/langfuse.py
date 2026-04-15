"""Langfuse tracer backend (optional dependency).

Requires the ``langfuse`` package::

    pip install langfuse
    # or: pip install bareagent[langfuse]

Activated automatically when ``LANGFUSE_PUBLIC_KEY`` is set, or when
``[tracing] langfuse = true`` appears in the config file.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from src.tracing._api import Span, Tracer


class LangfuseSpan(Span):
    """Span backed by a Langfuse generation or span object."""

    def __init__(self, langfuse_object: Any) -> None:
        self._lf = langfuse_object
        self._metadata: dict[str, Any] = {}
        self._error: str | None = None

    def set_tag(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def set_content_tag(self, key: str, value: Any) -> None:
        if key == "input":
            self._lf.input = value
        elif key == "output":
            self._lf.output = value
        else:
            self._metadata[key] = value

    def set_error(self, error: str) -> None:
        self._error = error

    def end(self) -> None:
        kwargs: dict[str, Any] = {}
        if self._metadata:
            kwargs["metadata"] = self._metadata
        if self._error:
            kwargs["level"] = "ERROR"
            kwargs["status_message"] = self._error

        # Langfuse generation objects accept usage on end()
        input_tokens = self._metadata.get("input_tokens")
        output_tokens = self._metadata.get("output_tokens")
        if input_tokens is not None or output_tokens is not None:
            kwargs["usage"] = {}
            if input_tokens is not None:
                kwargs["usage"]["input"] = int(input_tokens)
            if output_tokens is not None:
                kwargs["usage"]["output"] = int(output_tokens)

        self._lf.end(**kwargs)


class LangfuseTracer(Tracer):
    """Tracer that sends spans to Langfuse.

    Reads credentials from standard Langfuse environment variables
    (``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST``).
    """

    def __init__(
        self,
        *,
        session_id: str = "default",
        **langfuse_kwargs: Any,
    ) -> None:
        from langfuse import Langfuse

        self._langfuse = Langfuse(**langfuse_kwargs)
        self._session_id = session_id
        self._trace = self._langfuse.trace(
            name="bareagent-session",
            session_id=session_id,
        )
        self._current_span: LangfuseSpan | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session_id = value
        self._trace = self._langfuse.trace(
            name="bareagent-session",
            session_id=value,
        )

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        parent = (
            parent_span._lf  # type: ignore[union-attr]
            if isinstance(parent_span, LangfuseSpan)
            else self._trace
        )

        if operation_name == "llm_call":
            model = (tags or {}).get("model", "unknown")
            lf_obj = parent.generation(name=operation_name, model=model, metadata=tags)
        else:
            lf_obj = parent.span(name=operation_name, metadata=tags)

        span = LangfuseSpan(lf_obj)
        prev = self._current_span
        self._current_span = span
        try:
            yield span
        except Exception as exc:
            span.set_error(str(exc))
            raise
        finally:
            span.end()
            self._current_span = prev

    def current_span(self) -> Span | None:
        return self._current_span

    def flush(self) -> None:
        self._langfuse.flush()

    def shutdown(self) -> None:
        self._langfuse.flush()
        self._langfuse.shutdown()
