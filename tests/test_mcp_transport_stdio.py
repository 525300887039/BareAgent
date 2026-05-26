"""Tests for src.mcp.transport.stdio — subprocess-based NDJSON transport."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from src.mcp.errors import MCPTransportError
from src.mcp.protocol import Notification, Request
from src.mcp.transport import StdioTransport, Transport

# Minimal "MCP echo server": reads NDJSON requests on stdin, echoes them back
# wrapped as {"jsonrpc":"2.0","id":<same>,"result":{"echo":<params>}}. A line
# of "EXIT" causes a clean shutdown; a line of "NOTIFY" emits an unsolicited
# notification.
_ECHO_SERVER = r"""
import json
import sys

print("starting echo server", flush=True, file=sys.stderr)
# Also print a non-JSON banner to stdout to verify the client tolerates it.
print("# banner not JSON", flush=True)

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    if line == "EXIT":
        break
    if line == "NOTIFY":
        print(json.dumps({"jsonrpc": "2.0", "method": "ping/note", "params": {"hi": 1}}), flush=True)
        continue
    msg = json.loads(line)
    if "id" in msg:
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg.get("params")}}
        print(json.dumps(out), flush=True)
"""


def _make_transport() -> StdioTransport:
    return StdioTransport([sys.executable, "-u", "-c", _ECHO_SERVER])


def test_stdio_is_a_transport_subclass() -> None:
    assert issubclass(StdioTransport, Transport)


def test_stdio_request_response_round_trip() -> None:
    transport = _make_transport()
    transport.start()
    try:
        req = Request(id=1, method="ping", params={"hello": "world"})
        resp = transport.request(req, timeout=5.0)
        assert resp.id == 1
        assert resp.result == {"echo": {"hello": "world"}}
    finally:
        transport.close()


def test_stdio_concurrent_requests_route_correctly() -> None:
    transport = _make_transport()
    transport.start()
    try:
        results: dict[int, object] = {}
        errors: list[Exception] = []

        def send(req_id: int) -> None:
            try:
                req = Request(id=req_id, method="m", params={"n": req_id})
                resp = transport.request(req, timeout=5.0)
                results[req_id] = resp.result
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=send, args=(i,)) for i in range(1, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        assert len(results) == 5
        for i in range(1, 6):
            assert results[i] == {"echo": {"n": i}}
    finally:
        transport.close()


def test_stdio_subprocess_death_fails_all_pending() -> None:
    transport = _make_transport()
    transport.start()
    try:
        # Send a request and a kill signal: the kill arrives before any reply.
        # We start the request on a worker thread because send is synchronous.
        # Use the low-level mechanism: register a pending future, then kill.
        from concurrent.futures import Future

        future: Future = Future()
        with transport._pending_lock:  # noqa: SLF001 — internal test of routing
            transport._pending[99] = future  # noqa: SLF001

        # Hard-kill the subprocess (simulate crash).
        assert transport._proc is not None  # noqa: SLF001
        transport._proc.kill()  # noqa: SLF001

        with pytest.raises(MCPTransportError):
            future.result(timeout=5.0)

        # Subsequent requests should also fail loudly.
        with pytest.raises(MCPTransportError):
            transport.request(Request(id=100, method="x"), timeout=2.0)
    finally:
        transport.close()


def test_stdio_stderr_banner_does_not_break_pipe() -> None:
    transport = _make_transport()
    transport.start()
    try:
        # The echo server prints to stderr at startup; if that broke the reader,
        # the first request would hang. Just doing a round-trip is the assertion.
        resp = transport.request(
            Request(id=1, method="p", params={"k": "v"}), timeout=5.0
        )
        assert resp.result == {"echo": {"k": "v"}}
    finally:
        transport.close()


def test_stdio_notification_dispatched_to_callback() -> None:
    transport = _make_transport()
    transport.start()
    try:
        received: list[Notification] = []
        transport.on_notification(lambda n: received.append(n))
        # Tell echo server to emit one notification.
        transport.send("NOTIFY")
        # Give the reader a beat to dispatch.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)
        assert len(received) == 1
        assert received[0].method == "ping/note"
        assert received[0].params == {"hi": 1}
    finally:
        transport.close()


def test_stdio_is_alive_reflects_process_state() -> None:
    transport = _make_transport()
    transport.start()
    try:
        assert transport.is_alive() is True
    finally:
        transport.close()
    # After close the process should be dead.
    assert transport.is_alive() is False
