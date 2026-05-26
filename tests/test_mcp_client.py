"""Tests for src.mcp.client — single-server lifecycle and request dispatch."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from src.mcp.client import MCPClient
from src.mcp.config import MCPServerConfig
from src.mcp.errors import MCPCallError, MCPHandshakeError
from src.mcp.protocol import (
    ErrorObject,
    Notification,
    Request,
    Response,
    decode_message,
)
from src.mcp.transport.base import Transport


class FakeTransport(Transport):
    """In-memory transport: callers stage responses, sent messages are queued."""

    def __init__(self) -> None:
        super().__init__()
        self._started = False
        self._closed = False
        self.sent: list[str] = []
        self._responses_by_method: dict[str, Response | Exception] = {}
        self._auto_handle = True
        self._lock = threading.Lock()

    def queue_response_for(self, method: str, response: Response | Exception) -> None:
        self._responses_by_method[method] = response

    def disable_auto_handle(self) -> None:
        """Stop replying automatically — used to force a request timeout."""
        self._auto_handle = False

    def start(self) -> None:
        self._started = True

    def send(self, message: str) -> None:
        with self._lock:
            self.sent.append(message)
        msg = decode_message(message)
        if isinstance(msg, Request) and self._auto_handle:
            staged = self._responses_by_method.get(msg.method)
            if staged is None:
                return
            if isinstance(staged, Exception):
                # Surface as a transport error by routing nothing — caller times out.
                return
            staged.id = msg.id  # type: ignore[misc]
            self._route_response(staged)

    def close(self) -> None:
        self._closed = True

    def is_alive(self) -> bool:
        return self._started and not self._closed


def _make_config(name: str = "demo") -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command=["echo"])


def _ok_init_response(capabilities: dict[str, Any] | None = None) -> Response:
    return Response(
        id=0,
        result={
            "protocolVersion": "2025-06-18",
            "capabilities": capabilities or {"tools": {}},
            "serverInfo": {"name": "fake-server", "version": "1.0"},
        },
    )


def test_handshake_success_sends_initialized_notification() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    client = MCPClient(_make_config(), transport)

    client.start(timeout=1.0)

    # First message = initialize request; second = initialized notification.
    assert len(transport.sent) == 2
    init_msg = decode_message(transport.sent[0])
    assert isinstance(init_msg, Request) and init_msg.method == "initialize"
    notif_msg = decode_message(transport.sent[1])
    assert isinstance(notif_msg, Notification)
    assert notif_msg.method == "notifications/initialized"

    assert client.server_info["name"] == "fake-server"
    assert client.server_capabilities == {"tools": {}}
    assert client.is_alive() is True


def test_handshake_timeout_raises_and_closes_transport() -> None:
    transport = FakeTransport()
    transport.disable_auto_handle()
    client = MCPClient(_make_config(), transport)

    with pytest.raises(MCPHandshakeError):
        client.start(timeout=0.1)

    # The client closes the transport on handshake failure.
    assert transport._closed is True  # noqa: SLF001 — internal state check
    assert client.is_alive() is False


def test_handshake_server_error_raises() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize",
        Response(id=0, error=ErrorObject(code=-32603, message="boom")),
    )
    client = MCPClient(_make_config(), transport)

    with pytest.raises(MCPHandshakeError) as info:
        client.start(timeout=1.0)
    assert "boom" in str(info.value)
    assert transport._closed is True  # noqa: SLF001


def test_list_tools_succeeds_and_caches() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    transport.queue_response_for(
        "tools/list",
        Response(
            id=0,
            result={
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo the input",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    first = client.list_tools()
    assert first == [
        {
            "name": "echo",
            "description": "Echo the input",
            "inputSchema": {"type": "object"},
        }
    ]

    # Drop the staged response — cache should serve the second call.
    transport._responses_by_method.pop("tools/list", None)  # noqa: SLF001
    second = client.list_tools()
    assert second == first


def test_call_tool_success_returns_raw_result() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    transport.queue_response_for(
        "tools/call",
        Response(
            id=0,
            result={"content": [{"type": "text", "text": "hi"}], "isError": False},
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    result = client.call_tool("echo", {"msg": "hi"})
    assert result == {"content": [{"type": "text", "text": "hi"}], "isError": False}


def test_call_tool_jsonrpc_error_raises_mcp_call_error() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    transport.queue_response_for(
        "tools/call",
        Response(id=0, error=ErrorObject(code=-32602, message="bad params")),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    with pytest.raises(MCPCallError) as info:
        client.call_tool("echo", {})
    assert "-32602" in str(info.value)
    assert "bad params" in str(info.value)
    assert str(info.value).startswith("MCP Error:")


def test_call_tool_is_error_true_returns_normally() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    transport.queue_response_for(
        "tools/call",
        Response(
            id=0,
            result={
                "content": [{"type": "text", "text": "rate limit"}],
                "isError": True,
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    result = client.call_tool("flaky", {})
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "rate limit"}]


def test_list_tools_skipped_when_server_omits_tools_capability() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"resources": {}})
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    assert client.list_tools() == []
    methods = [decode_message(line) for line in transport.sent]
    # Only initialize + initialized notification were sent — no tools/list.
    assert all(
        not (isinstance(m, Request) and m.method == "tools/list") for m in methods
    )


def test_close_is_idempotent() -> None:
    transport = FakeTransport()
    transport.queue_response_for("initialize", _ok_init_response())
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)
    client.close()
    client.close()  # second call must not raise
    assert client.is_alive() is False
