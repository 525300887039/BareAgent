"""MCP server configuration parsing.

Reads a `[mcp]` block (plus `[[mcp.servers]]` array) from a TOML-derived dict
and returns typed dataclasses. The transport field selects which server-side
fields are required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import MCPError

_VALID_TRANSPORTS = ("stdio", "http_legacy", "http_streamable")

# 256 KiB. The text result of a single MCP tool call lands directly in the
# next LLM turn; anything significantly larger blows past the typical
# context window before the model even sees the rest of the turn.
_DEFAULT_MAX_TEXT_BYTES = 262_144  # 256 KiB
_DEFAULT_MAX_BINARY_BYTES = 5_242_880  # 5 MiB
_DEFAULT_START_TIMEOUT = 10.0


@dataclass(slots=True)
class MCPServerConfig:
    """One MCP server entry. Required fields depend on transport."""

    name: str
    transport: str
    # stdio fields:
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # http_* fields:
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # shared:
    start_timeout: float = _DEFAULT_START_TIMEOUT


@dataclass(slots=True)
class MCPConfig:
    """Top-level MCP configuration."""

    servers: list[MCPServerConfig] = field(default_factory=list)
    max_result_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES
    max_result_binary_bytes: int = _DEFAULT_MAX_BINARY_BYTES
    start_timeout: float = _DEFAULT_START_TIMEOUT


def parse_mcp_config(raw: dict[str, Any]) -> MCPConfig:
    """Parse a TOML-derived dict (the `[mcp]` section) into MCPConfig.

    Accepts either the full document (where `mcp` is a key) or the `[mcp]`
    block itself. Unknown keys are silently ignored to stay forward-compatible.
    """
    if not isinstance(raw, dict):
        raise MCPError(f"mcp config must be a table, got {type(raw).__name__}")

    block = raw.get("mcp", raw)
    if not isinstance(block, dict):
        raise MCPError("'mcp' must be a table")

    cfg = MCPConfig(
        max_result_text_bytes=_int(block, "max_result_text_bytes", _DEFAULT_MAX_TEXT_BYTES),
        max_result_binary_bytes=_int(block, "max_result_binary_bytes", _DEFAULT_MAX_BINARY_BYTES),
        start_timeout=_float(block, "start_timeout", _DEFAULT_START_TIMEOUT),
    )

    servers_raw = block.get("servers", [])
    if not isinstance(servers_raw, list):
        raise MCPError("'mcp.servers' must be an array of tables")

    seen: set[str] = set()
    for index, entry in enumerate(servers_raw):
        if not isinstance(entry, dict):
            raise MCPError(f"mcp.servers[{index}] must be a table")
        server = _parse_server(entry, index, default_start_timeout=cfg.start_timeout)
        if server.name in seen:
            raise MCPError(f"duplicate mcp server name: {server.name!r}")
        seen.add(server.name)
        cfg.servers.append(server)
    return cfg


def _parse_server(
    entry: dict[str, Any], index: int, *, default_start_timeout: float
) -> MCPServerConfig:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise MCPError(f"mcp.servers[{index}].name is required and must be a non-empty string")

    transport = entry.get("transport")
    if transport not in _VALID_TRANSPORTS:
        raise MCPError(
            f"mcp.servers[{name}].transport must be one of {_VALID_TRANSPORTS}, got {transport!r}"
        )

    server = MCPServerConfig(
        name=name,
        transport=transport,
        start_timeout=_float(entry, "start_timeout", default_start_timeout),
    )

    if transport == "stdio":
        command = entry.get("command")
        if isinstance(command, str):
            server.command = [command]
        elif isinstance(command, list) and all(isinstance(s, str) for s in command):
            server.command = list(command)
        else:
            raise MCPError(f"mcp.servers[{name}].command is required for stdio transport")
        if not server.command:
            raise MCPError(f"mcp.servers[{name}].command must not be empty")
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(s, str) for s in args):
            raise MCPError(f"mcp.servers[{name}].args must be a list of strings")
        server.args = list(args)
        env = entry.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise MCPError(f"mcp.servers[{name}].env must be a string->string table")
        server.env = dict(env)
        cwd = entry.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise MCPError(f"mcp.servers[{name}].cwd must be a string if provided")
        server.cwd = cwd
    else:
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise MCPError(f"mcp.servers[{name}].url is required for transport {transport!r}")
        server.url = url
        headers = entry.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise MCPError(f"mcp.servers[{name}].headers must be a string->string table")
        server.headers = dict(headers)

    return server


def _int(block: dict[str, Any], key: str, default: int) -> int:
    value = block.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MCPError(f"mcp.{key} must be an integer, got {value!r}")
    return value


def _float(block: dict[str, Any], key: str, default: float) -> float:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPError(f"mcp.{key} must be a number, got {value!r}")
    return float(value)
