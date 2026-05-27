"""MCP 2024-11-05 HTTP+SSE transport (two endpoints).

Connection lifecycle:

1. `start()` opens a long-lived SSE GET to `url`. The first SSE event MUST be
   `event: endpoint`, with `data` = the POST endpoint URL (a plain string,
   not JSON). That URL becomes the write target.
2. `send()` POSTs each JSON-RPC envelope to the captured endpoint.
3. All server -> client traffic (responses + notifications) arrives as
   `event: message` SSE events whose data field is one JSON-RPC envelope.
"""

from __future__ import annotations

import logging
import threading
from urllib.parse import urljoin

import httpx

from .._sse import parse_sse_stream
from ..errors import MCPProtocolError, MCPTransportError
from ..protocol import Notification, Request, Response, decode_message
from .base import Transport

_log = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_PROTOCOL_HEADERS_GET = {
    "Accept": "text/event-stream",
    "MCP-Protocol-Version": _PROTOCOL_VERSION,
}
_PROTOCOL_HEADERS_POST = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": _PROTOCOL_VERSION,
}


class HttpLegacyTransport(Transport):
    """Two-endpoint MCP HTTP transport (GET SSE + POST writes)."""

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
        self._response: httpx.Response | None = None
        self._stream_cm = None  # type: ignore[var-annotated]
        self._reader: threading.Thread | None = None
        self._endpoint_url: str | None = None
        self._endpoint_event = threading.Event()
        self._closed = False
        # Reader uses ``_closing`` to skip the disconnect handler when shutdown
        # is user-initiated. Set by ``close()`` before tearing down the stream.
        self._closing = False

    def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("HttpLegacyTransport already started")
        timeout = httpx.Timeout(
            connect=self._start_timeout, read=None, write=10.0, pool=10.0
        )
        self._client = httpx.Client(timeout=timeout)
        headers = self._merge_headers(_PROTOCOL_HEADERS_GET)
        try:
            self._stream_cm = self._client.stream("GET", self._url, headers=headers)
            self._response = self._stream_cm.__enter__()
            self._response.raise_for_status()
        except httpx.HTTPError as exc:
            self._cleanup()
            raise MCPTransportError(f"failed to open SSE stream: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-http-legacy-reader", daemon=True
        )
        self._reader.start()

        if not self._endpoint_event.wait(timeout=self._start_timeout):
            self.close()
            raise MCPTransportError(
                "did not receive endpoint event within start timeout"
            )

    def send(self, message: str) -> None:
        if self._closed:
            raise MCPTransportError("transport is closed")
        if self._client is None or self._endpoint_url is None:
            raise MCPTransportError("transport not started")
        headers = self._merge_headers(_PROTOCOL_HEADERS_POST)
        try:
            resp = self._client.post(
                self._endpoint_url, headers=headers, content=message.encode("utf-8")
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MCPTransportError(f"POST failed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        self._cleanup()
        self._fail_all_pending("HTTP legacy transport closed")

    def is_alive(self) -> bool:
        return (
            not self._closed
            and self._client is not None
            and self._endpoint_url is not None
        )

    # --- internals ---

    def _merge_headers(self, base: dict[str, str]) -> dict[str, str]:
        # User-supplied headers must not override protocol-mandated ones.
        merged = dict(self._user_headers)
        merged.update(base)
        return merged

    def _cleanup(self) -> None:
        if self._stream_cm is not None:
            try:
                self._stream_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._stream_cm = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def _read_loop(self) -> None:
        assert self._response is not None
        disconnect_reason: str | None = None
        try:
            for event in parse_sse_stream(self._response.iter_lines()):
                if event["event"] == "endpoint":
                    self._on_endpoint(event["data"])
                elif event["event"] == "message":
                    self._on_message(event["data"])
                else:
                    _log.warning(
                        "MCP http_legacy: unknown SSE event %r", event["event"]
                    )
        except httpx.HTTPError as exc:
            disconnect_reason = f"SSE stream broken: {exc}"
            _log.warning("MCP http_legacy: SSE stream broken: %s", exc)
        except Exception as exc:  # pragma: no cover — defensive
            disconnect_reason = f"reader crashed: {exc}"
            _log.warning("MCP http_legacy reader crashed: %s", exc)
        finally:
            if not self._closing:
                self._invoke_disconnect(
                    disconnect_reason or "SSE stream ended unexpectedly"
                )
            self._fail_all_pending("SSE stream closed (server disconnect or error)")

    def _on_endpoint(self, data: str) -> None:
        # data is a relative or absolute URL string; resolve against the base URL.
        endpoint = data.strip()
        if not endpoint:
            _log.warning("MCP http_legacy: empty endpoint event")
            return
        self._endpoint_url = urljoin(self._url, endpoint)
        self._endpoint_event.set()

    def _on_message(self, data: str) -> None:
        try:
            msg = decode_message(data)
        except MCPProtocolError as exc:
            _log.warning("MCP http_legacy: bad message event: %s", exc)
            return
        if isinstance(msg, Response):
            self._route_response(msg)
        elif isinstance(msg, Notification):
            self._route_notification(msg)
        else:
            # Server-to-client Request: not handled in PR1.
            assert isinstance(msg, Request)
            _log.warning(
                "MCP http_legacy: ignoring server-to-client request %r", msg.method
            )
