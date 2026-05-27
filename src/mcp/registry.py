"""MCP -> BareAgent tool schema + handler injection.

Each running MCP server contributes its tools to the BareAgent tool list under
the ``mcp__<server>__<tool>`` namespace. ``inputSchema`` is forwarded
verbatim: research shows real-world servers stick to a small core of standard
JSON Schema keywords (``$ref`` / ``$defs`` / ``anyOf`` / ``enum`` / ``default``),
and provider SDKs accept them. The registry deliberately does NOT normalize
or strip; that is the LLM provider's job.

PR3 additionally injects two synthetic tools per server that declares the
``resources`` capability: ``mcp__<server>__resource_list`` (no args) and
``mcp__<server>__resource_read`` (single ``uri`` arg). LLMs use these to
discover and fetch MCP resources without needing system-prompt injection.
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

# PR3 still flattens only the ``text`` content type natively. Other kinds
# (image / audio / embedded resource / resource_link) become a placeholder so
# the LLM at least sees something arrived; PR5 will wire these through
# core.loop._tool_result for multimodal passthrough.
_PR5_OMITTED_TYPES = {"image", "audio", "resource", "resource_link"}

_RESOURCE_LIST_SUFFIX = "resource_list"
_RESOURCE_READ_SUFFIX = "resource_read"


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
            tools = []
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

        # Resource discovery + fetch tools — only when the server opted into them.
        if _client_has_capability(client, "resources"):
            for schema in _resource_tool_schemas(server_name):
                full_name = schema["name"]
                if full_name in seen:
                    raise MCPError(
                        f"MCP tool name collision after namespacing: {full_name!r}"
                    )
                seen.add(full_name)
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
            tools = []
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

        if _client_has_capability(client, "resources"):
            list_name = mcp_tool_name(server_name, _RESOURCE_LIST_SUFFIX)
            read_name = mcp_tool_name(server_name, _RESOURCE_READ_SUFFIX)
            handlers[list_name] = _make_resource_list_handler(manager, server_name)
            handlers[read_name] = _make_resource_read_handler(manager, server_name)
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


def _make_resource_list_handler(
    manager: MCPManager,
    server_name: str,
) -> Callable[..., str]:
    """Closure: ``resources/list`` -> a human-readable multi-line string."""

    def _handler(**_kwargs: Any) -> str:
        client = manager.get_client(server_name)
        if client is None:
            return f"Error: MCP server {server_name!r} is not running"
        try:
            resources = client.list_resources()
        except MCPCallError as exc:
            return str(exc)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
        if not resources:
            return "(no resources)"
        lines: list[str] = []
        for resource in resources:
            uri = str(resource.get("uri", ""))
            name = str(resource.get("name", ""))
            description = str(resource.get("description", ""))
            mime_type = str(resource.get("mimeType", ""))
            lines.append(f"{uri} | {name} | {description} | {mime_type}")
        return "\n".join(lines)

    _handler.__name__ = f"mcp_handler_{server_name}_resource_list"
    return _handler


def _make_resource_read_handler(
    manager: MCPManager,
    server_name: str,
) -> Callable[..., str]:
    """Closure: ``resources/read`` -> flattened content text (with PR5 placeholders)."""

    def _handler(uri: str | None = None, **_kwargs: Any) -> str:
        client = manager.get_client(server_name)
        if client is None:
            return f"Error: MCP server {server_name!r} is not running"
        if not isinstance(uri, str) or not uri:
            return "Error: resource_read requires a non-empty 'uri' argument"
        try:
            result = client.read_resource(uri)
        except MCPCallError as exc:
            return str(exc)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
        contents = result.get("contents")
        body = _flatten_content(contents if isinstance(contents, list) else [])
        if result.get("isError"):
            return f"Error: {body}" if body else "Error: (no content)"
        return body

    _handler.__name__ = f"mcp_handler_{server_name}_resource_read"
    return _handler


def _resource_tool_schemas(server_name: str) -> list[dict[str, Any]]:
    return [
        {
            "name": mcp_tool_name(server_name, _RESOURCE_LIST_SUFFIX),
            "description": (
                f"List available resources from MCP server {server_name!r}"
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": mcp_tool_name(server_name, _RESOURCE_READ_SUFFIX),
            "description": (f"Read a resource from MCP server {server_name!r}"),
            "input_schema": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "Resource URI to read",
                    }
                },
                "required": ["uri"],
            },
        },
    ]


def _flatten_content(content: list[dict[str, Any]]) -> str:
    """Stringify an MCP content array (tools/call, resources/read, prompts).

    ``text`` blocks are concatenated verbatim with a newline between them. Any
    other block becomes a ``[<type> omitted: PR5]`` placeholder so the LLM at
    least sees that data arrived. PR5 will replace this with multimodal
    passthrough.
    """
    parts: list[str] = []
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
    return "\n".join(parts)


def _flatten_result(result: dict[str, Any]) -> str:
    """Stringify a ``tools/call`` result into a single BareAgent tool-output string.

    PR2 supports the ``text`` content type natively; other kinds become a
    ``[<type> omitted: PR5]`` placeholder so the LLM at least sees something
    arrived. ``isError: true`` prepends the canonical ``Error: `` prefix and
    keeps the content text so the model can decide how to react.
    """
    content = result.get("content")
    body = _flatten_content(content if isinstance(content, list) else [])
    if result.get("isError"):
        return f"Error: {body}" if body else "Error: (no content)"
    return body


def _client_has_capability(client: Any, name: str) -> bool:
    """Best-effort capability check that tolerates MagicMock-style fakes in tests."""
    check = getattr(client, "has_capability", None)
    if not callable(check):
        return False
    try:
        return bool(check(name))
    except Exception:
        return False


def _coerce_input_schema(schema: Any) -> dict[str, Any]:
    """Pass through the MCP ``inputSchema``, defaulting to an empty object schema."""
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}
