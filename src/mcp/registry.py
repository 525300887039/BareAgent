"""MCP -> BareAgent tool schema + handler injection.

Each running MCP server contributes its tools to the BareAgent tool list under
the ``mcp__<server>__<tool>`` namespace. ``inputSchema`` is forwarded
verbatim: research shows real-world servers stick to a small core of standard
JSON Schema keywords (``$ref`` / ``$defs`` / ``anyOf`` / ``enum`` / ``default``),
and provider SDKs accept them. The registry deliberately does NOT normalize
or strip; that is the LLM provider's job.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import MCPCallError, MCPError

if TYPE_CHECKING:
    from .manager import MCPManager

_log = logging.getLogger(__name__)

_NAME_SEPARATOR = "__"
_NAME_PREFIX = "mcp"

# PR2 only flattens text content. Other content kinds become placeholder text
# until PR5 wires multimodal passthrough into core.loop._tool_result.
_PR5_OMITTED_TYPES = {"image", "audio", "resource", "resource_link"}


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build the BareAgent-visible tool name (``mcp__<server>__<tool>``)."""
    return f"{_NAME_PREFIX}{_NAME_SEPARATOR}{server_name}{_NAME_SEPARATOR}{tool_name}"


def build_mcp_tool_schemas(manager: MCPManager) -> list[dict[str, Any]]:
    """Produce BareAgent-shaped tool schemas for every running MCP tool.

    Raises ``MCPError`` if two servers expose tools that collide after the
    ``mcp__<server>__<tool>`` rewrite — duplicate server names are already
    prevented by config parsing, so the only realistic collision is a server
    returning the same tool name twice in its ``tools/list``.
    """
    schemas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for server_name, client in manager.iter_running_clients():
        try:
            tools = client.list_tools()
        except Exception as exc:
            _log.warning(
                "MCP server %r tools/list failed during schema build: %s",
                server_name,
                exc,
            )
            continue
        seen_in_server: set[str] = set()
        for tool in tools:
            original_name = tool.get("name")
            if not isinstance(original_name, str) or not original_name:
                continue
            if original_name in seen_in_server:
                _log.warning(
                    "MCP server %r returned duplicate tool name %r; keeping the first",
                    server_name,
                    original_name,
                )
                continue
            seen_in_server.add(original_name)
            full_name = mcp_tool_name(server_name, original_name)
            if full_name in seen:
                raise MCPError(
                    f"MCP tool name collision after namespacing: {full_name!r}"
                )
            seen.add(full_name)
            schema: dict[str, Any] = {
                "name": full_name,
                "description": tool.get("description") or "",
                "input_schema": _coerce_input_schema(tool.get("inputSchema")),
            }
            schemas.append(schema)
    return schemas


def build_mcp_handlers(manager: MCPManager) -> dict[str, Callable[..., Any]]:
    """Produce ``{tool_name: handler}`` callables that forward to the right client.

    Handlers swallow ``MCPCallError`` and ``isError: true`` results into the
    BareAgent error-as-text convention so the agent loop never crashes on a
    misbehaving MCP server.
    """
    handlers: dict[str, Callable[..., Any]] = {}
    for server_name, client in manager.iter_running_clients():
        try:
            tools = client.list_tools()
        except Exception as exc:
            _log.warning(
                "MCP server %r tools/list failed during handler build: %s",
                server_name,
                exc,
            )
            continue
        seen_in_server: set[str] = set()
        for tool in tools:
            original_name = tool.get("name")
            if not isinstance(original_name, str) or not original_name:
                continue
            if original_name in seen_in_server:
                continue
            seen_in_server.add(original_name)
            full_name = mcp_tool_name(server_name, original_name)
            handlers[full_name] = _make_handler(manager, server_name, original_name)
    return handlers


def _make_handler(
    manager: MCPManager,
    server_name: str,
    original_tool_name: str,
) -> Callable[..., str]:
    """Closure: looks up the live client at call-time so reload/crashes show up."""

    def _handler(**kwargs: Any) -> str:
        client = manager.get_client(server_name)
        if client is None:
            return f"Error: MCP server {server_name!r} is not running"
        try:
            result = client.call_tool(original_tool_name, kwargs)
        except MCPCallError as exc:
            return str(exc)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
        return _flatten_result(result)

    _handler.__name__ = f"mcp_handler_{server_name}_{original_tool_name}"
    return _handler


def _flatten_result(result: dict[str, Any]) -> str:
    """Stringify a ``tools/call`` result into a single BareAgent tool-output string.

    PR2 supports the ``text`` content type natively; other kinds become a
    ``[<type> omitted: PR5]`` placeholder so the LLM at least sees something
    arrived. ``isError: true`` prepends the canonical ``Error: `` prefix and
    keeps the content text so the model can decide how to react.
    """
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif block_type in _PR5_OMITTED_TYPES:
                parts.append(f"[{block_type} omitted: PR5]")
            else:
                parts.append(f"[{block_type or 'unknown'} omitted: PR5]")
    body = "\n".join(parts)
    if result.get("isError"):
        return f"Error: {body}" if body else "Error: (no content)"
    return body


def _coerce_input_schema(schema: Any) -> dict[str, Any]:
    """Pass through the MCP ``inputSchema``, defaulting to an empty object schema."""
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}
