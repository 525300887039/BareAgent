"""Tests for src.mcp.config — TOML-derived MCP configuration parsing."""

from __future__ import annotations

import pytest

from bareagent.mcp.config import MCPConfig, parse_mcp_config
from bareagent.mcp.errors import MCPError


def _wrap(servers: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
    return {"mcp": {"servers": servers, **kwargs}}


def test_parses_stdio_http_legacy_and_streamable() -> None:
    raw = _wrap(
        [
            {
                "name": "fs",
                "transport": "stdio",
                "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"],
                "args": ["/tmp"],
                "env": {"DEBUG": "1"},
            },
            {
                "name": "remote-legacy",
                "transport": "http_legacy",
                "url": "https://example.com/sse",
                "headers": {"Authorization": "Bearer x"},
            },
            {
                "name": "remote-stream",
                "transport": "http_streamable",
                "url": "https://example.com/mcp",
            },
        ],
        max_result_text_bytes=2048,
        max_result_binary_bytes=8192,
        start_timeout=15.0,
    )
    cfg = parse_mcp_config(raw)
    assert isinstance(cfg, MCPConfig)
    assert cfg.max_result_text_bytes == 2048
    assert cfg.max_result_binary_bytes == 8192
    assert cfg.start_timeout == 15.0
    assert [s.name for s in cfg.servers] == ["fs", "remote-legacy", "remote-stream"]
    fs = cfg.servers[0]
    assert fs.transport == "stdio"
    assert fs.command == ["npx", "-y", "@modelcontextprotocol/server-filesystem"]
    assert fs.args == ["/tmp"]
    assert fs.env == {"DEBUG": "1"}
    assert fs.start_timeout == 15.0
    legacy = cfg.servers[1]
    assert legacy.transport == "http_legacy"
    assert legacy.url == "https://example.com/sse"
    assert legacy.headers == {"Authorization": "Bearer x"}
    stream = cfg.servers[2]
    assert stream.transport == "http_streamable"
    assert stream.url == "https://example.com/mcp"


def test_accepts_block_directly_without_outer_mcp_key() -> None:
    cfg = parse_mcp_config(
        {"servers": [{"name": "a", "transport": "stdio", "command": ["cat"]}]}
    )
    assert cfg.servers[0].name == "a"


def test_missing_command_for_stdio_raises() -> None:
    with pytest.raises(MCPError, match="command is required"):
        parse_mcp_config(_wrap([{"name": "x", "transport": "stdio"}]))


def test_empty_command_for_stdio_raises() -> None:
    with pytest.raises(MCPError, match="command"):
        parse_mcp_config(_wrap([{"name": "x", "transport": "stdio", "command": []}]))


def test_missing_url_for_http_raises() -> None:
    with pytest.raises(MCPError, match="url is required"):
        parse_mcp_config(_wrap([{"name": "x", "transport": "http_legacy"}]))


def test_unknown_transport_raises() -> None:
    with pytest.raises(MCPError, match="transport must be one of"):
        parse_mcp_config(_wrap([{"name": "x", "transport": "websocket"}]))


def test_missing_name_raises() -> None:
    with pytest.raises(MCPError, match="name is required"):
        parse_mcp_config(_wrap([{"transport": "stdio", "command": ["x"]}]))


def test_duplicate_server_name_raises() -> None:
    raw = _wrap(
        [
            {"name": "dup", "transport": "stdio", "command": ["a"]},
            {"name": "dup", "transport": "stdio", "command": ["b"]},
        ]
    )
    with pytest.raises(MCPError, match="duplicate"):
        parse_mcp_config(raw)


def test_servers_must_be_a_list() -> None:
    with pytest.raises(MCPError, match="must be an array of tables"):
        parse_mcp_config({"mcp": {"servers": {}}})


def test_command_as_string_normalized_to_list() -> None:
    cfg = parse_mcp_config(
        _wrap([{"name": "x", "transport": "stdio", "command": "uvx mcp"}])
    )
    assert cfg.servers[0].command == ["uvx mcp"]


def test_defaults_when_no_mcp_block() -> None:
    cfg = parse_mcp_config({})
    assert cfg.servers == []
    assert cfg.max_result_text_bytes > 0
    assert cfg.start_timeout > 0


def test_invalid_env_table_raises() -> None:
    with pytest.raises(MCPError, match="env"):
        parse_mcp_config(
            _wrap(
                [{"name": "x", "transport": "stdio", "command": ["c"], "env": {"k": 1}}]
            )
        )
