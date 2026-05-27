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


# --- PR3: capability parsing + prompts + resources -------------------------


def test_capability_parsing_records_prompts_and_resources() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize",
        _ok_init_response(capabilities={"prompts": {}, "resources": {}}),
    )
    transport.queue_response_for(
        "prompts/list",
        Response(id=0, result={"prompts": []}),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    assert client.has_capability("prompts") is True
    assert client.has_capability("resources") is True
    assert client.has_capability("tools") is False


def test_start_skips_prompts_list_when_capability_absent() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"tools": {}})
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    # _prompts stays None — server did not opt into prompts.
    assert client._prompts is None  # noqa: SLF001
    methods = [decode_message(line) for line in transport.sent]
    assert all(
        not (isinstance(m, Request) and m.method == "prompts/list") for m in methods
    )


def test_start_caches_prompts_list_when_capability_present() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize",
        _ok_init_response(capabilities={"prompts": {"listChanged": True}}),
    )
    transport.queue_response_for(
        "prompts/list",
        Response(
            id=0,
            result={
                "prompts": [
                    {
                        "name": "summarize",
                        "description": "Summarize a URL",
                        "arguments": [{"name": "url", "required": True}],
                    }
                ]
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    assert client.list_prompts() == [
        {
            "name": "summarize",
            "description": "Summarize a URL",
            "arguments": [{"name": "url", "required": True}],
        }
    ]


def test_start_filters_prompts_with_illegal_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prompt names outside [a-zA-Z0-9_-] would break the /mcp: REPL syntax."""
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"prompts": {}})
    )
    transport.queue_response_for(
        "prompts/list",
        Response(
            id=0,
            result={
                "prompts": [
                    {"name": "good_name"},
                    {"name": "also-fine"},
                    {"name": "bad name"},  # space — illegal
                    {"name": "bad:name"},  # colon — illegal (clashes with /mcp:)
                ]
            },
        ),
    )
    client = MCPClient(_make_config(), transport)

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="src.mcp.client"):
        client.start(timeout=1.0)

    names = {p["name"] for p in client.list_prompts()}
    assert names == {"good_name", "also-fine"}
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno >= 30]
    assert any("bad name" in msg for msg in warnings)
    assert any("bad:name" in msg for msg in warnings)


def test_start_swallows_prompts_list_error_without_failing_handshake() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"prompts": {}})
    )
    transport.queue_response_for(
        "prompts/list",
        Response(id=0, error=ErrorObject(code=-32603, message="boom")),
    )
    client = MCPClient(_make_config(), transport)

    client.start(timeout=1.0)

    # Handshake still succeeded; cache fell back to empty so list_prompts works.
    assert client.is_alive() is True
    assert client.list_prompts() == []


def test_get_prompt_success_returns_raw_messages() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"prompts": {}})
    )
    transport.queue_response_for(
        "prompts/list",
        Response(id=0, result={"prompts": [{"name": "summarize"}]}),
    )
    transport.queue_response_for(
        "prompts/get",
        Response(
            id=0,
            result={
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": "Summarize https://x"},
                    }
                ],
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    result = client.get_prompt("summarize", {"url": "https://x"})
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"]["text"] == "Summarize https://x"


def test_get_prompt_jsonrpc_error_raises_mcp_call_error() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"prompts": {}})
    )
    transport.queue_response_for("prompts/list", Response(id=0, result={"prompts": []}))
    transport.queue_response_for(
        "prompts/get",
        Response(id=0, error=ErrorObject(code=-32602, message="missing arg")),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    with pytest.raises(MCPCallError) as info:
        client.get_prompt("summarize", {})
    assert "-32602" in str(info.value)
    assert "missing arg" in str(info.value)


def test_list_resources_success_returns_array() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"resources": {}})
    )
    transport.queue_response_for(
        "resources/list",
        Response(
            id=0,
            result={
                "resources": [
                    {"uri": "file:///a.txt", "name": "A", "mimeType": "text/plain"},
                    {"uri": "file:///b.bin", "name": "B"},
                ]
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    resources = client.list_resources()
    assert len(resources) == 2
    assert resources[0]["uri"] == "file:///a.txt"


def test_list_resources_jsonrpc_error_raises_mcp_call_error() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"resources": {}})
    )
    transport.queue_response_for(
        "resources/list",
        Response(id=0, error=ErrorObject(code=-32603, message="server kaput")),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    with pytest.raises(MCPCallError) as info:
        client.list_resources()
    assert "server kaput" in str(info.value)


def test_read_resource_returns_is_error_payload_without_raising() -> None:
    transport = FakeTransport()
    transport.queue_response_for(
        "initialize", _ok_init_response(capabilities={"resources": {}})
    )
    transport.queue_response_for(
        "resources/read",
        Response(
            id=0,
            result={
                "contents": [
                    {"type": "text", "text": "boom: file is missing"},
                ],
                "isError": True,
            },
        ),
    )
    client = MCPClient(_make_config(), transport)
    client.start(timeout=1.0)

    result = client.read_resource("file:///gone.txt")
    assert result["isError"] is True
    assert result["contents"][0]["text"] == "boom: file is missing"
