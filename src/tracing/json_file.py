"""JSON file tracer -- wraps InteractionLogger via composition."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from src.debug.interaction_log import InteractionLogger
from src.tracing._api import Span, Tracer


class JsonFileSpan(Span):
    """Span that accumulates tags for the JSON file backend."""

    def __init__(self, operation_name: str) -> None:
        self.operation_name = operation_name
        self._tags: dict[str, Any] = {}
        self._content_tags: dict[str, Any] = {}
        self._error: str | None = None

    def set_tag(self, key: str, value: Any) -> None:
        self._tags[key] = value

    def set_content_tag(self, key: str, value: Any) -> None:
        self._content_tags[key] = value

    def set_error(self, error: str) -> None:
        self._error = error

    def end(self) -> None:
        pass


class JsonFileTracer(Tracer):
    """Tracer backed by InteractionLogger -- full backward compat.

    The underlying ``InteractionLogger`` is exposed via the ``.logger``
    property so that the web viewer and ``/log`` commands can continue
    using it directly.  All ``InteractionLogger`` public methods are
    also proxied on this class so that duck-typed call sites (e.g.
    ``_safe_log_request`` in *agent_loop*) work transparently.
    """

    def __init__(self, logger: InteractionLogger) -> None:
        self._logger = logger
        self._current_span: JsonFileSpan | None = None

    @property
    def logger(self) -> InteractionLogger:
        """Expose the underlying logger for web_viewer / /log commands."""
        return self._logger

    @contextlib.contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        *,
        parent_span: Span | None = None,
    ) -> Iterator[Span]:
        span = JsonFileSpan(operation_name)
        if tags:
            for k, v in tags.items():
                span.set_tag(k, v)
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

    # ---- Delegation: InteractionLogger methods for /log and web_viewer ----

    @property
    def session_id(self) -> str:
        return self._logger.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._logger.session_id = value

    def log_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        provider_info: dict[str, Any] | None = None,
    ) -> int:
        return self._logger.log_request(messages, tools, provider_info=provider_info)

    def log_response(self, seq: int, **kwargs: Any) -> None:
        self._logger.log_response(seq, **kwargs)

    def list_sessions(self) -> list[str]:
        return self._logger.list_sessions()

    def list_interactions(self, session_id: str) -> list[dict[str, Any]]:
        return self._logger.list_interactions(session_id)

    def get_interaction(self, session_id: str, seq: int) -> dict[str, Any]:
        return self._logger.get_interaction(session_id, seq)

    def subscribe_events(self, **kwargs: Any) -> Any:
        return self._logger.subscribe_events(**kwargs)

    def unsubscribe_events(self, event_queue: Any) -> None:
        return self._logger.unsubscribe_events(event_queue)
