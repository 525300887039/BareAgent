"""Transport ABC + shared id-routing machinery."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import Future

from ..errors import MCPProtocolError, MCPTransportError
from ..protocol import Notification, Request, Response, encode_message

NotificationCallback = Callable[[Notification], None]
DisconnectCallback = Callable[[str], None]

_log = logging.getLogger(__name__)


class Transport(ABC):
    """Abstract bidirectional message channel for one MCP server.

    Concrete subclasses (stdio, http_legacy, http_streamable) implement
    `start` / `send` / `close` / `is_alive`. The shared `request` / `notify`
    helpers plus the pending-future routing live here so all transports route
    server responses identically.

    Subclasses must distinguish ``graceful close`` (user called ``close()``)
    from ``unexpected disconnect`` (subprocess EOF, broken pipe, SSE stream
    error) and only invoke the registered disconnect handler in the
    unexpected case. See :meth:`_invoke_disconnect`.
    """

    def __init__(self) -> None:
        self._pending: dict[int, Future[Response]] = {}
        self._pending_lock = threading.Lock()
        self._notification_callbacks: list[NotificationCallback] = []
        self._callbacks_lock = threading.Lock()
        self._disconnect_handler: DisconnectCallback | None = None
        self._disconnect_invoked = False

    @abstractmethod
    def start(self) -> None:
        """Open the underlying connection / launch the subprocess."""

    @abstractmethod
    def send(self, message: str) -> None:
        """Write a single already-encoded JSON-RPC message."""

    @abstractmethod
    def close(self) -> None:
        """Release all resources (subprocess, sockets, threads)."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Return True while the underlying transport is healthy."""

    def request(self, request: Request, *, timeout: float) -> Response:
        """Send a request and block until the matching response arrives."""
        future: Future[Response] = Future()
        with self._pending_lock:
            if request.id in self._pending:
                raise MCPProtocolError(f"request id {request.id} already in flight")
            self._pending[request.id] = future
        try:
            self.send(encode_message(request))
        except BaseException:
            with self._pending_lock:
                self._pending.pop(request.id, None)
            raise
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise MCPProtocolError(f"request {request.id} timed out after {timeout}s") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request.id, None)

    def notify(self, notification: Notification) -> None:
        """Send a notification (no response expected)."""
        self.send(encode_message(notification))

    def on_notification(self, callback: NotificationCallback) -> None:
        """Register a server->client notification callback."""
        with self._callbacks_lock:
            self._notification_callbacks.append(callback)

    def set_disconnect_handler(self, callback: DisconnectCallback | None) -> None:
        """Register a one-shot callback for unexpected transport disconnects.

        Called by subclass reader threads when they detect EOF / broken pipe /
        unexpected stream termination — never on user-initiated ``close()``.
        Passing ``None`` clears the handler.
        """
        self._disconnect_handler = callback

    # --- internal hooks used by subclass readers ---

    def _route_response(self, response: Response) -> None:
        """Deliver a response to its waiting future, or drop it if abandoned."""
        if response.id is None:
            return  # parse-error responses with null id have no waiter
        with self._pending_lock:
            future = self._pending.get(response.id)
        if future is None or future.done():
            return  # orphan / late response — ignore per JSON-RPC convention
        future.set_result(response)

    def _route_notification(self, notification: Notification) -> None:
        """Fan out a server->client notification to registered callbacks."""
        with self._callbacks_lock:
            callbacks = list(self._notification_callbacks)
        for cb in callbacks:
            try:
                cb(notification)
            except Exception:
                # Callbacks must not break the reader thread.
                pass

    def _fail_all_pending(self, message: str) -> None:
        """Signal connection loss to every in-flight request."""
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _, future in pending:
            if not future.done():
                future.set_exception(MCPTransportError(message))

    def _invoke_disconnect(self, reason: str) -> None:
        """Fire the disconnect handler exactly once on unexpected termination.

        Subclasses call this from their reader thread when they detect an
        unexpected disconnect (EOF, broken pipe, SSE stream broken, subprocess
        died). Calls after the first one are no-ops so a reader that detects
        the same condition twice does not double-notify the manager. A
        graceful ``close()`` must not call this.
        """
        if self._disconnect_invoked:
            return
        self._disconnect_invoked = True
        handler = self._disconnect_handler
        if handler is None:
            return
        try:
            handler(reason)
        except Exception as exc:  # pragma: no cover — handler must never crash reader
            _log.warning("MCP transport disconnect handler raised: %s", exc)
