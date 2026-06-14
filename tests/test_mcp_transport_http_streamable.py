"""Tests for src.mcp.transport.http_streamable — MCP 2025-03-26 single-endpoint HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bareagent.mcp.protocol import Request
from bareagent.mcp.transport import HttpStreamableTransport, Transport


class _StreamableServer:
    """In-process single-endpoint HTTP server emulating MCP 2025-03-26.

    - GET returns 405 (no listening stream).
    - POST replies with application/json containing an echo response.
    - Emits a session id on the first POST response, and verifies subsequent
      POSTs carry that session id.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.session_id = "test-session-1"
        self.observed_sessions: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: D401
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(405)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                msg = json.loads(body)
                outer.posts.append(msg)
                outer.observed_sessions.append(self.headers.get("Mcp-Session-Id"))
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"echo": msg.get("params")},
                    }
                )
                data = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Mcp-Session-Id", outer.session_id)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _StreamableSseServer:
    """Same as above but returns the POST response as a text/event-stream body."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: D401
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(405)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                msg = json.loads(body)
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"echo": msg.get("params")},
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def json_server():
    server = _StreamableServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def sse_server():
    server = _StreamableSseServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_http_streamable_is_transport_subclass() -> None:
    assert issubclass(HttpStreamableTransport, Transport)


def test_http_streamable_json_post_round_trip(json_server: _StreamableServer) -> None:
    transport = HttpStreamableTransport(json_server.url)
    transport.start()
    try:
        resp = transport.request(
            Request(id=1, method="ping", params={"a": 1}), timeout=5.0
        )
        assert resp.id == 1
        assert resp.result == {"echo": {"a": 1}}
        # First POST has no session header; second carries the captured id.
        resp2 = transport.request(
            Request(id=2, method="ping", params={"a": 2}), timeout=5.0
        )
        assert resp2.id == 2
        assert json_server.observed_sessions[0] is None
        assert json_server.observed_sessions[1] == json_server.session_id
    finally:
        transport.close()


def test_http_streamable_sse_post_round_trip(sse_server: _StreamableSseServer) -> None:
    transport = HttpStreamableTransport(sse_server.url)
    transport.start()
    try:
        resp = transport.request(
            Request(id=1, method="ping", params={"a": 1}), timeout=5.0
        )
        assert resp.id == 1
        assert resp.result == {"echo": {"a": 1}}
    finally:
        transport.close()
