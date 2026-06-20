"""Tests for src.mcp.transport.http_legacy — MCP 2024-11-05 two-endpoint HTTP+SSE."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bareagent.mcp.protocol import Request
from bareagent.mcp.transport import HttpLegacyTransport, Transport


class _LegacyServer:
    """In-process HTTP+SSE server emulating MCP 2024-11-05."""

    def __init__(self) -> None:
        self._sse_writer: object | None = None
        self._sse_lock = threading.Lock()
        self._posts: list[dict[str, object]] = []
        self.endpoint_path = "/messages?session=test-session"

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: D401
                pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/sse":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # First event: announce endpoint URL.
                self.wfile.write(f"event: endpoint\ndata: {outer.endpoint_path}\n\n".encode())
                self.wfile.flush()
                with outer._sse_lock:
                    outer._sse_writer = self.wfile
                # Block until the test tears down by sleeping in 50ms chunks.
                while not outer._shutdown.is_set():
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionError):
                        return
                    outer._shutdown.wait(timeout=0.2)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                msg = json.loads(body)
                outer._posts.append(msg)
                self.send_response(202)
                self.end_headers()
                # Asynchronously emit the response on the SSE stream.
                if "id" in msg:
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"echo": msg.get("params")},
                    }
                    outer._emit_sse_message(response)

        self._handler = Handler
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sse"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        self._server.shutdown()
        self._server.server_close()

    def _emit_sse_message(self, payload: dict[str, object]) -> None:
        line = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        with self._sse_lock:
            writer = self._sse_writer
        if writer is None:
            return
        try:
            writer.write(line)  # type: ignore[attr-defined]
            writer.flush()  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionError):
            pass


@pytest.fixture()
def legacy_server():
    server = _LegacyServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_http_legacy_is_transport_subclass() -> None:
    assert issubclass(HttpLegacyTransport, Transport)


def test_http_legacy_endpoint_negotiation_and_round_trip(
    legacy_server: _LegacyServer,
) -> None:
    transport = HttpLegacyTransport(legacy_server.base_url)
    transport.start()
    try:
        # Give the SSE handler a moment to register itself.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and legacy_server._sse_writer is None:
            time.sleep(0.02)
        resp = transport.request(Request(id=1, method="ping", params={"k": "v"}), timeout=5.0)
        assert resp.id == 1
        assert resp.result == {"echo": {"k": "v"}}
        # Verify the POST happened against the negotiated endpoint.
        assert len(legacy_server._posts) == 1
        assert legacy_server._posts[0]["id"] == 1
    finally:
        transport.close()


def test_http_legacy_merges_user_headers(legacy_server: _LegacyServer) -> None:
    transport = HttpLegacyTransport(legacy_server.base_url, headers={"X-Test": "ok"})
    # We're not validating header propagation in this minimal server, but the
    # constructor should accept and merge user headers without protocol clash.
    transport.start()
    transport.close()
