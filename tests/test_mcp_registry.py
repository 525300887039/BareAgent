"""Tests for src.mcp.registry — tool schema injection + handler routing."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bareagent.mcp.errors import MCPCallError, MCPError
from bareagent.mcp.manager import ServerStatus
from bareagent.mcp.registry import (
    _flatten_content,
    build_mcp_handlers,
    build_mcp_tool_schemas,
    mcp_tool_name,
)


def _fake_manager(
    clients: dict[str, MagicMock],
    statuses: dict[str, ServerStatus] | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like MCPManager for the registry.

    Each client mock defaults ``has_capability(...)`` to ``False`` — opt into
    resources/prompts injection by setting ``client.has_capability.return_value``
    or ``side_effect`` in the test body.
    """
    manager = MagicMock()
    effective_status = dict(statuses or {})
    for name in clients:
        effective_status.setdefault(name, ServerStatus.RUNNING)

    for client in clients.values():
        # MagicMock auto-creates truthy child mocks — pin has_capability to a
        # plain False return so PR3 resource tool injection only fires when a
        # test explicitly opts in by overriding return_value / side_effect.
        client.has_capability.return_value = False

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
    """PR5: success path returns ``list[dict]`` of content blocks (text-only here)."""
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
    assert result == [
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ]
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


def test_handler_returns_multimodal_blocks_on_success() -> None:
    """PR5: tools/call success path returns the multimodal ``list[dict]`` shape.

    The legacy ``[<type> omitted: PR5]`` string degradation only kicks in on
    error / ``isError`` paths now; success keeps the structured content.
    """
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
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "label:"}
    assert out[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "ZmFrZQ=="},
    }
    assert out[2] == {
        "type": "text",
        "text": "[Audio omitted: not supported by current providers]",
    }


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


# --- PR3: resources capability injection ----------------------------------


def _resource_capable_client(
    tools: list[dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.list_tools.return_value = tools or []
    client.has_capability.side_effect = lambda name: name == "resources"
    return client


def test_resource_tools_injected_only_when_capability_declared() -> None:
    plain = MagicMock()
    plain.list_tools.return_value = []
    plain.has_capability.return_value = False

    capable = _resource_capable_client()

    manager = _fake_manager({"plain": plain, "fs": capable})
    schemas = build_mcp_tool_schemas(manager)
    names = {schema["name"] for schema in schemas}

    assert "mcp__fs__resource_list" in names
    assert "mcp__fs__resource_read" in names
    assert "mcp__plain__resource_list" not in names
    assert "mcp__plain__resource_read" not in names


def test_resource_read_schema_requires_uri() -> None:
    capable = _resource_capable_client()
    manager = _fake_manager({"fs": capable})

    schemas = build_mcp_tool_schemas(manager)
    read_schema = next(s for s in schemas if s["name"] == "mcp__fs__resource_read")
    assert read_schema["input_schema"]["required"] == ["uri"]
    assert read_schema["input_schema"]["properties"]["uri"]["type"] == "string"


def test_resource_list_handler_flattens_resources_to_multiline_string() -> None:
    capable = _resource_capable_client()
    capable.list_resources.return_value = [
        {
            "uri": "file:///a.txt",
            "name": "A",
            "description": "First",
            "mimeType": "text/plain",
        },
        {"uri": "file:///b.bin", "name": "B"},
    ]
    manager = _fake_manager({"fs": capable})
    handler = build_mcp_handlers(manager)["mcp__fs__resource_list"]

    out = handler()
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == "file:///a.txt | A | First | text/plain"
    assert lines[1] == "file:///b.bin | B |  | "


def test_resource_list_handler_returns_error_on_mcp_call_error() -> None:
    capable = _resource_capable_client()
    capable.list_resources.side_effect = MCPCallError("MCP Error: -32603 down")
    manager = _fake_manager({"fs": capable})

    handler = build_mcp_handlers(manager)["mcp__fs__resource_list"]
    assert handler() == "MCP Error: -32603 down"


def test_resource_list_handler_returns_error_when_server_unhealthy() -> None:
    capable = _resource_capable_client()
    manager = _fake_manager({"fs": capable})
    handlers = build_mcp_handlers(manager)
    manager.get_client.side_effect = lambda name: None  # noqa: ARG005

    out = handlers["mcp__fs__resource_list"]()
    assert out.startswith("Error: ")
    assert "fs" in out


def test_resource_read_handler_success_returns_content_blocks() -> None:
    """PR5: resources/read success path returns ``list[dict]`` content blocks."""
    capable = _resource_capable_client()
    capable.read_resource.return_value = {
        "contents": [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ],
        "isError": False,
    }
    manager = _fake_manager({"fs": capable})
    handler = build_mcp_handlers(manager)["mcp__fs__resource_read"]

    out = handler(uri="file:///a.txt")
    assert out == [
        {"type": "text", "text": "line one"},
        {"type": "text", "text": "line two"},
    ]
    capable.read_resource.assert_called_once_with("file:///a.txt")


def test_resource_read_handler_translates_unknown_kind_to_placeholder() -> None:
    """Unknown block kinds (e.g. legacy ``blob``) degrade to a text placeholder."""
    capable = _resource_capable_client()
    capable.read_resource.return_value = {
        "contents": [
            {"type": "text", "text": "preamble"},
            {
                "type": "blob",
                "blob": "ZmFrZQ==",
                "mimeType": "application/octet-stream",
            },
        ],
        "isError": False,
    }
    manager = _fake_manager({"fs": capable})
    handler = build_mcp_handlers(manager)["mcp__fs__resource_read"]

    out = handler(uri="file:///a.bin")
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "preamble"}
    assert out[1] == {"type": "text", "text": "[Unknown content block: blob]"}


def test_resource_read_handler_prefixes_is_error_payload() -> None:
    capable = _resource_capable_client()
    capable.read_resource.return_value = {
        "contents": [{"type": "text", "text": "permission denied"}],
        "isError": True,
    }
    manager = _fake_manager({"fs": capable})
    handler = build_mcp_handlers(manager)["mcp__fs__resource_read"]

    out = handler(uri="file:///secret")
    assert out == "Error: permission denied"


def test_resource_read_handler_returns_error_on_mcp_call_error() -> None:
    capable = _resource_capable_client()
    capable.read_resource.side_effect = MCPCallError("MCP Error: -32602 bad uri")
    manager = _fake_manager({"fs": capable})

    handler = build_mcp_handlers(manager)["mcp__fs__resource_read"]
    assert handler(uri="file:///x") == "MCP Error: -32602 bad uri"


def test_resource_read_handler_rejects_missing_uri() -> None:
    capable = _resource_capable_client()
    manager = _fake_manager({"fs": capable})

    handler = build_mcp_handlers(manager)["mcp__fs__resource_read"]
    out = handler()
    assert out.startswith("Error:")
    assert "uri" in out


# --- PR6: payload truncation at the normalization boundary ----------------


def test_to_content_blocks_text_under_threshold_passes_through() -> None:
    """PR6: a text block within the configured byte cap is preserved verbatim."""
    from bareagent.mcp.config import MCPConfig
    from bareagent.mcp.registry import _to_content_blocks

    cfg = MCPConfig(max_result_text_bytes=262_144)
    text = "a" * 250 * 1024  # 250 KiB, under the 256 KiB cap
    out = _to_content_blocks([{"type": "text", "text": text}], config=cfg)
    assert out == [{"type": "text", "text": text}]


def test_to_content_blocks_text_over_threshold_is_truncated() -> None:
    """PR6: a text block above the byte cap is sliced and tagged so the LLM
    can detect the truncation and act on it (retry / paginate)."""
    from bareagent.mcp.config import MCPConfig
    from bareagent.mcp.registry import _to_content_blocks

    cfg = MCPConfig(max_result_text_bytes=262_144)
    text = "a" * (257 * 1024)  # 257 KiB, just past the 256 KiB cap
    original_bytes = len(text.encode("utf-8"))
    out = _to_content_blocks([{"type": "text", "text": text}], config=cfg)
    assert len(out) == 1
    rendered = out[0]["text"]
    assert rendered.endswith(f"[truncated, original size: {original_bytes} bytes]")
    # Body before the suffix is exactly the byte cap (chars are 1-byte ASCII).
    body = rendered.split("\n[truncated", 1)[0]
    assert len(body.encode("utf-8")) == 262_144


def test_to_content_blocks_image_over_binary_threshold_is_omitted() -> None:
    """PR6: an image whose base64 decodes to > max_result_binary_bytes is
    replaced with an LLM-readable placeholder (no decode allocation)."""
    from bareagent.mcp.config import MCPConfig
    from bareagent.mcp.registry import _to_content_blocks

    cfg = MCPConfig(max_result_binary_bytes=5_242_880)  # 5 MiB
    # 6 MiB of decoded payload ≈ 8 MiB base64 string. We approximate by
    # generating a base64 string of the right length without actually
    # decoding the bytes anywhere.
    target_decoded = 6 * 1024 * 1024
    b64 = "A" * ((target_decoded * 4) // 3)
    out = _to_content_blocks(
        [{"type": "image", "data": b64, "mimeType": "image/png"}], config=cfg
    )
    assert len(out) == 1
    assert out[0]["type"] == "text"
    assert "Resource omitted: too large" in out[0]["text"]


def test_to_content_blocks_image_under_binary_threshold_passes_through() -> None:
    """Sanity check: an image well below the binary cap is not degraded."""
    from bareagent.mcp.config import MCPConfig
    from bareagent.mcp.registry import _to_content_blocks

    cfg = MCPConfig(max_result_binary_bytes=5_242_880)
    out = _to_content_blocks(
        [{"type": "image", "data": "AAAA", "mimeType": "image/png"}], config=cfg
    )
    assert out == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        }
    ]


def test_flatten_content_with_config_truncates_text() -> None:
    """PR6: ``_flatten_content`` applies the same text cap on error paths so
    huge error payloads do not bypass the truncation contract."""
    from bareagent.mcp.config import MCPConfig
    from bareagent.mcp.registry import _flatten_content

    cfg = MCPConfig(max_result_text_bytes=1024)
    blob = "x" * 2048
    out = _flatten_content([{"type": "text", "text": blob}], config=cfg)
    assert out.endswith("[truncated, original size: 2048 bytes]")
    body = out.split("\n[truncated", 1)[0]
    assert len(body.encode("utf-8")) == 1024


def test_flatten_content_without_config_is_unchanged() -> None:
    """Back-compat: callers that omit the config (transcript injection, etc.)
    still get the legacy unbounded behavior."""
    from bareagent.mcp.registry import _flatten_content

    blob = "y" * 2048
    out = _flatten_content([{"type": "text", "text": blob}])
    assert out == blob


def test_flatten_content_handles_text_other_and_empty() -> None:
    assert _flatten_content([]) == ""
    assert (
        _flatten_content(
            [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ]
        )
        == "a\nb"
    )
    assert _flatten_content([{"type": "image", "data": "x"}]) == "[image omitted: PR5]"
    assert (
        _flatten_content([{"type": "weird"}, {"type": "text", "text": "ok"}])
        == "[weird omitted: PR5]\nok"
    )
    # Non-dict blocks are ignored, not crashed on.
    assert _flatten_content([None, {"type": "text", "text": "x"}]) == "x"  # type: ignore[list-item]
