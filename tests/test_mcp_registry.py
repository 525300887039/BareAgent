"""Tests for src.mcp.registry — tool schema injection + handler routing."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.mcp.errors import MCPCallError, MCPError
from src.mcp.manager import ServerStatus
from src.mcp.registry import (
    build_mcp_handlers,
    build_mcp_tool_schemas,
    mcp_tool_name,
)


def _fake_manager(
    clients: dict[str, MagicMock],
    statuses: dict[str, ServerStatus] | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like MCPManager for the registry."""
    manager = MagicMock()
    effective_status = dict(statuses or {})
    for name in clients:
        effective_status.setdefault(name, ServerStatus.RUNNING)

    def _iter_running() -> Any:
        return iter(
            [
                (name, client)
                for name, client in clients.items()
                if effective_status.get(name) == ServerStatus.RUNNING
            ]
        )

    def _get_client(name: str) -> Any:
        if effective_status.get(name) != ServerStatus.RUNNING:
            return None
        return clients.get(name)

    manager.iter_running_clients.side_effect = _iter_running
    manager.get_client.side_effect = _get_client
    return manager


def test_tool_name_prefix_format() -> None:
    assert mcp_tool_name("fs", "read") == "mcp__fs__read"


def test_build_schemas_injects_namespaced_tools() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {
            "name": "fetch",
            "description": "Get URL",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        }
    ]
    manager = _fake_manager({"web": client})

    schemas = build_mcp_tool_schemas(manager)
    assert len(schemas) == 1
    assert schemas[0]["name"] == "mcp__web__fetch"
    assert schemas[0]["description"] == "Get URL"
    assert schemas[0]["input_schema"]["properties"]["url"] == {"type": "string"}


def test_build_handlers_forwards_text_content() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "echo", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.return_value = {
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ],
        "isError": False,
    }
    manager = _fake_manager({"srv": client})

    handlers = build_mcp_handlers(manager)
    assert "mcp__srv__echo" in handlers
    result = handlers["mcp__srv__echo"](anything="x")
    assert result == "hello \nworld"
    client.call_tool.assert_called_once_with("echo", {"anything": "x"})


def test_handler_returns_error_string_when_server_unhealthy() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "echo", "description": "", "inputSchema": {"type": "object"}}
    ]
    manager = _fake_manager({"srv": client})
    handlers = build_mcp_handlers(manager)

    # Mark srv unhealthy AFTER handlers are built; handler should detect at call time.
    manager.get_client.side_effect = lambda name: None  # noqa: ARG005

    out = handlers["mcp__srv__echo"]()
    assert out.startswith("Error: ")
    assert "srv" in out


def test_handler_translates_non_text_blocks_to_placeholder() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "art", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.return_value = {
        "content": [
            {"type": "text", "text": "label:"},
            {"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"},
            {"type": "audio", "data": "ZmFrZQ==", "mimeType": "audio/wav"},
        ],
        "isError": False,
    }
    manager = _fake_manager({"s": client})
    handler = build_mcp_handlers(manager)["mcp__s__art"]

    out = handler()
    assert "label:" in out
    assert "[image omitted: PR5]" in out
    assert "[audio omitted: PR5]" in out


def test_handler_adds_error_prefix_on_is_error_true() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "flaky", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.return_value = {
        "content": [{"type": "text", "text": "rate limited"}],
        "isError": True,
    }
    manager = _fake_manager({"s": client})
    handler = build_mcp_handlers(manager)["mcp__s__flaky"]

    out = handler()
    assert out == "Error: rate limited"


def test_handler_catches_mcp_call_error_as_string() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "boom", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.side_effect = MCPCallError("MCP Error: -32602 bad params")
    manager = _fake_manager({"s": client})
    handler = build_mcp_handlers(manager)["mcp__s__boom"]

    out = handler()
    assert out == "MCP Error: -32602 bad params"


def test_schema_passes_through_ref_and_defs_unchanged() -> None:
    """Zod-flavoured schemas (with $ref + $defs) must not be modified."""
    nested_schema = {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": {"$ref": "#/$defs/Entity"}}
        },
        "$defs": {
            "Entity": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "memorize", "description": "", "inputSchema": nested_schema}
    ]
    manager = _fake_manager({"m": client})

    schemas = build_mcp_tool_schemas(manager)
    assert schemas[0]["input_schema"] == nested_schema


def test_schema_passes_through_pydantic_anyof_null() -> None:
    """Pydantic Optional → anyOf:[T,null] must not be normalized."""
    schema = {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string"},
            "start_ts": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            },
        },
        "required": ["repo_path"],
    }
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "git_log", "description": "", "inputSchema": schema}
    ]
    manager = _fake_manager({"g": client})

    schemas = build_mcp_tool_schemas(manager)
    assert schemas[0]["input_schema"] == schema


def test_duplicate_tool_name_within_server_warns_but_keeps_first() -> None:
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "echo", "description": "first", "inputSchema": {"type": "object"}},
        {"name": "echo", "description": "second", "inputSchema": {"type": "object"}},
    ]
    manager = _fake_manager({"s": client})

    schemas = build_mcp_tool_schemas(manager)
    assert len(schemas) == 1
    assert schemas[0]["description"] == "first"


def test_namespaced_collision_across_servers_raises() -> None:
    """If somehow two namespaced names match (config dedup already prevents server
    name collisions, but this guards regressions in registry build-up)."""
    client_a = MagicMock()
    client_a.list_tools.return_value = [
        {"name": "x", "description": "", "inputSchema": {"type": "object"}}
    ]
    # Force the same namespaced name to collide via a doctored manager.
    client_b = MagicMock()
    client_b.list_tools.return_value = [
        {"name": "x", "description": "", "inputSchema": {"type": "object"}}
    ]
    # Use the same server name for both — that mimics the post-namespacing
    # collision case the registry must detect.
    manager = MagicMock()
    manager.iter_running_clients.side_effect = lambda: iter(
        [("dup", client_a), ("dup", client_b)]
    )

    with pytest.raises(MCPError):
        build_mcp_tool_schemas(manager)
