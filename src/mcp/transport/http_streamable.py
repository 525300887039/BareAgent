"""MCP 2025-03-26 Streamable HTTP transport (single endpoint).

Key differences vs HTTP legacy:

- One URL handles both directions. POST writes go to `url`; the server
  responds with either `application/json` (a single envelope) or
  `text/event-stream` (one or more `event: message` envelopes then closes).
- An optional long-lived GET stream is opened for server -> client
  notifications. If the server returns 405 / 404 for the GET we silently
  fall back to POST-only mode.
- Session continuity uses the `Mcp-Session-Id` response header echoed on
  every subsequent request.
"""

from __future__ import annotations

import logging
import threading

import httpx

from .._sse import parse_sse_stream
from ..errors import MCPProtocolError, MCPTransportError
from ..protocol import Notification, Request, Response, decode_message
from .base import Transport

_log = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_SESSION_HEADER = "Mcp-Session-Id"
_PROTOCOL_HEADERS_BASE = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": _PROTOCOL_VERSION,
}
_PROTOCOL_HEADERS_POST = {**_PROTOCOL_HEADERS_BASE, "Content-Type": "application/json"}


class HttpStreamableTransport(Transport):
    """Single-endpoint MCP HTTP transport per the 2025-03-26 spec."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        start_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self._url = url
        self._user_headers = dict(headers or {})
        self._start_timeout = start_timeout
        self._client: httpx.Client | None = None
        self._listen_response: httpx.Response | None = None
        self._listen_cm = None  # type: ignore[var-annotated]
        self._listen_thread: threading.Thread | None = None
        self._session_id: str | None = None
        self._session_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("HttpStreamableTransport already started")
        timeout = httpx.Timeout(
            connect=self._start_timeout, read=None, write=10.0, pool=10.0
        )
        self._client = httpx.Client(timeout=timeout)

        # Best-effort GET for server-push notifications. Servers may refuse.
        try:
            headers = self._merge_headers(_PROTOCOL_HEADERS_BASE)
            self._listen_cm = self._client.stream("GET", self._url, headers=headers)
            response = self._listen_cm.__enter__()
            if response.status_code >= 400:
                # Server doesn't offer a listening stream; that's fine for POST-only.
                self._listen_cm.__exit__(None, None, None)
                self._listen_cm = None
                self._listen_response = None
            else:
                self._capture_session(response)
                self._listen_response = response
                self._listen_thread = threading.Thread(
                    target=self._listen_loop, name="mcp-http-stream-reader", daemon=True
                )
                self._listen_thread.start()
        except httpx.HTTPError as exc:
            # Listening stream is optional — log and continue with POST-only mode.
            _log.warning("MCP http_streamable: GET listen stream unavailable: %s", exc)
            if self._listen_cm is not None:
                try:
                    self._listen_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._listen_cm = None

    def send(self, message: str) -> None:
        if self._closed:
            raise MCPTransportError("transport is closed")
        if self._client is None:
            raise MCPTransportError("transport not started")
        headers = self._merge_headers(_PROTOCOL_HEADERS_POST)
        try:
            resp = self._client.post(
                self._url, headers=headers, content=message.encode("utf-8")
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MCPTransportError(f"POST failed: {exc}") from exc

        self._capture_session(resp)
        self._handle_response_body(resp)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._listen_cm is not None:
            try:
                self._listen_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._listen_cm = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._fail_all_pending("HTTP streamable transport closed")

    def is_alive(self) -> bool:
        return not self._closed and self._client is not None

    # --- internals ---

    def _merge_headers(self, base: dict[str, str]) -> dict[str, str]:
        merged = dict(self._user_headers)
        merged.update(base)
        with self._session_lock:
            if self._session_id is not None:
                merged[_SESSION_HEADER] = self._session_id
        return merged

    def _capture_session(self, response: httpx.Response) -> None:
        session = response.headers.get(_SESSION_HEADER)
        if session:
            with self._session_lock:
                self._session_id = session

    def _handle_response_body(self, response: httpx.Response) -> None:
        # POST body can be either a single JSON envelope or an SSE stream.
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/event-stream" in content_type:
            try:
                for event in parse_sse_stream(response.iter_lines()):
                    if event["event"] == "message":
                        self._on_message(event["data"])
                    else:
                        _log.warning(
                            "MCP http_streamable: unknown SSE event %r in POST response",
                            event["event"],
                        )
            except httpx.HTTPError as exc:
                _log.warning("MCP http_streamable: POST SSE stream broken: %s", exc)
            return
        if not response.content:
            return  # 202 Accepted with empty body — response will arrive on listen stream
        if "application/json" in content_type or response.content.lstrip().startswith(
            b"{"
        ):
            self._on_message(response.text)
            return
        _log.warning("MCP http_streamable: unexpected Content-Type %r", content_type)

    def _listen_loop(self) -> None:
        assert self._listen_response is not None
        try:
            for event in parse_sse_stream(self._listen_response.iter_lines()):
                if event["event"] == "message":
                    self._on_message(event["data"])
                else:
                    _log.warning(
                        "MCP http_streamable: unknown SSE event %r on listen stream",
                        event["event"],
                    )
        except httpx.HTTPError as exc:
            _log.warning("MCP http_streamable: listen stream broken: %s", exc)
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning("MCP http_streamable listener crashed: %s", exc)
        finally:
            self._fail_all_pending("HTTP streamable listen stream closed")

    def _on_message(self, data: str) -> None:
        try:
            msg = decode_message(data)
        except MCPProtocolError as exc:
            _log.warning("MCP http_streamable: bad message: %s", exc)
            return
        if isinstance(msg, Response):
            self._route_response(msg)
        elif isinstance(msg, Notification):
            self._route_notification(msg)
        else:
            assert isinstance(msg, Request)
            _log.warning(
                "MCP http_streamable: ignoring server-to-client request %r", msg.method
            )
