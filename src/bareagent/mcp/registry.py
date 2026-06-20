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

PR5 wires multimodal results through. ``tools/call`` and ``resources/read``
success paths now return a ``list[dict]`` of BareAgent-internal content
blocks (Anthropic-native ``image`` shape + text placeholders for other
modalities). Error paths still degrade to the legacy ``str`` form so the
loop's existing string handling continues to work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .config import MCPConfig
from .errors import MCPCallError, MCPError

if TYPE_CHECKING:
    from .manager import MCPManager

_log = logging.getLogger(__name__)

_NAME_SEPARATOR = "__"
_NAME_PREFIX = "mcp"

# Legacy ``_flatten_content`` (still used for error paths and prompts/transcript
# injection) renders every non-text block as ``[<type> omitted: PR5]``. The
# real multimodal path now goes through :func:`_to_content_blocks`.
_PR5_OMITTED_TYPES = {"image", "audio", "resource", "resource_link"}

# Anthropic Messages API accepts only these mime types in tool_result image
# blocks; anything else is degraded to a text placeholder so we never push a
# payload the API will reject.
_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_RESOURCE_LIST_SUFFIX = "resource_list"
_RESOURCE_READ_SUFFIX = "resource_read"

# Truncation suffix templates. Stable text so trellis-check / tests can match
# them verbatim and the LLM can reliably detect that it was truncated and
# adjust its next request (e.g. ask for less / paginate).
_TEXT_TRUNCATED_SUFFIX = "\n[truncated, original size: {n} bytes]"
_RESOURCE_OMITTED_TEMPLATE = "[Resource omitted: too large ({n} bytes)]"


def _truncate_text(text: str, max_bytes: int) -> str:
    """Return ``text`` truncated to fit within ``max_bytes`` UTF-8 bytes.

    Appends ``[truncated, original size: N bytes]`` when truncation kicks in
    so the LLM knows the payload was cut. ``max_bytes <= 0`` disables the
    cap (helpful in tests that construct a bare ``MCPConfig``).
    """
    if max_bytes <= 0:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    original_size = len(encoded)
    # Slice at byte boundary, then decode with replacement so we never split a
    # multibyte character mid-codepoint.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + _TEXT_TRUNCATED_SUFFIX.format(n=original_size)


def _estimate_base64_bytes(data: str) -> int:
    """Estimate the decoded size of a base64 string without allocating it.

    Each 4 base64 chars decode to 3 bytes (minus padding). Approximation is
    accurate to within ~2 bytes, plenty precise for a single-threshold check
    and *much* cheaper than ``base64.b64decode`` on a 6 MiB payload.
    """
    if not data:
        return 0
    padding = data.count("=", max(0, len(data) - 2))
    return (len(data) * 3) // 4 - padding


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
                raise MCPError(f"MCP tool name collision after namespacing: {full_name!r}")
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
                    raise MCPError(f"MCP tool name collision after namespacing: {full_name!r}")
                seen.add(full_name)
                schemas.append(schema)
    return schemas


def build_mcp_handlers(manager: MCPManager) -> dict[str, Callable[..., Any]]:
    """Produce ``{tool_name: handler}`` callables that forward to the right client.

    Handlers swallow ``MCPCallError`` and ``isError: true`` results into the
    BareAgent error-as-text convention so the agent loop never crashes on a
    misbehaving MCP server. The active :class:`MCPConfig` is captured by each
    handler closure so :func:`_to_content_blocks` can enforce the configured
    text / binary truncation thresholds without round-tripping through the
    manager on every call.
    """
    handlers: dict[str, Callable[..., Any]] = {}
    # MagicMock-based test managers may not expose a real ``MCPConfig`` — fall
    # back to ``None`` (= no truncation) so the registry stays usable in unit
    # tests without forcing every fixture to wire a config.
    raw_config = getattr(manager, "config", None)
    config = raw_config if isinstance(raw_config, MCPConfig) else None
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
            handlers[full_name] = _make_handler(manager, server_name, original_name, config)

        if _client_has_capability(client, "resources"):
            list_name = mcp_tool_name(server_name, _RESOURCE_LIST_SUFFIX)
            read_name = mcp_tool_name(server_name, _RESOURCE_READ_SUFFIX)
            handlers[list_name] = _make_resource_list_handler(manager, server_name)
            handlers[read_name] = _make_resource_read_handler(manager, server_name, config)
    return handlers


def _make_handler(
    manager: MCPManager,
    server_name: str,
    original_tool_name: str,
    config: MCPConfig | None,
) -> Callable[..., str | list[dict[str, Any]]]:
    """Closure: looks up the live client at call-time so reload/crashes show up.

    On the success path the handler returns the multimodal ``list[dict]`` shape
    (BareAgent-internal content blocks); on any failure or ``isError: true`` it
    falls back to the legacy ``str`` form so the agent loop's stringify path
    keeps working unchanged.
    """

    def _handler(**kwargs: Any) -> str | list[dict[str, Any]]:
        client = manager.get_client(server_name)
        if client is None:
            return f"Error: MCP server {server_name!r} is not running"
        try:
            result = client.call_tool(original_tool_name, kwargs)
        except MCPCallError as exc:
            return str(exc)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
        content = result.get("content")
        content_list = content if isinstance(content, list) else []
        if result.get("isError"):
            body = _flatten_content(content_list, config=config)
            return f"Error: {body}" if body else "Error: (no content)"
        return _to_content_blocks(content_list, config=config)

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
    config: MCPConfig | None,
) -> Callable[..., str | list[dict[str, Any]]]:
    """Closure: ``resources/read`` -> multimodal content blocks on success.

    Mirrors :func:`_make_handler`: success returns ``list[dict]`` so binary
    resources (e.g. images) reach the LLM intact; error / ``isError: true``
    paths still return a ``str`` prefixed with ``Error:``.
    """

    def _handler(uri: str | None = None, **_kwargs: Any) -> str | list[dict[str, Any]]:
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
        contents_list = contents if isinstance(contents, list) else []
        if result.get("isError"):
            body = _flatten_content(contents_list, config=config)
            return f"Error: {body}" if body else "Error: (no content)"
        return _to_content_blocks(contents_list, config=config)

    _handler.__name__ = f"mcp_handler_{server_name}_resource_read"
    return _handler


def _resource_tool_schemas(server_name: str) -> list[dict[str, Any]]:
    return [
        {
            "name": mcp_tool_name(server_name, _RESOURCE_LIST_SUFFIX),
            "description": (f"List available resources from MCP server {server_name!r}"),
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


def _flatten_content(
    content: list[dict[str, Any]],
    *,
    config: MCPConfig | None = None,
) -> str:
    """Stringify an MCP content array (tools/call, resources/read, prompts).

    ``text`` blocks are concatenated verbatim with a newline between them. Any
    other block becomes a ``[<type> omitted: PR5]`` placeholder. PR5 keeps this
    helper around for error paths (where we want a single error string) and
    for prompts / transcript injection (where the consumer is a chat message
    that must be plain text).

    When ``config`` is supplied the final joined string is truncated to fit
    ``config.max_result_text_bytes`` so error payloads (which the LLM will see
    next turn) stay within the same envelope as success payloads.
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
    joined = "\n".join(parts)
    if config is not None:
        joined = _truncate_text(joined, config.max_result_text_bytes)
    return joined


def _to_content_blocks(
    mcp_content: list[dict[str, Any]],
    *,
    config: MCPConfig | None = None,
) -> list[dict[str, Any]]:
    """Normalize an MCP ``content`` array into BareAgent-internal content blocks.

    Output blocks use Anthropic's native shape so the Anthropic provider can
    forward them verbatim. The OpenAI provider lifts image blocks into a
    follow-up user message at serialization time (see
    ``OpenAIProvider._convert_non_assistant_message``).

    Conversions:

    - ``{type: "text", text}``                       → ``{type: "text", text}``
    - ``{type: "image", data, mimeType}``
      → ``{type: "image", source: {type: "base64", media_type, data}}``
      (only when ``mimeType`` is in the Anthropic-supported whitelist and
      ``data`` is non-empty; otherwise degraded to a text placeholder.)
    - ``{type: "audio", ...}``                       → text placeholder
    - ``{type: "embedded_resource", resource: {uri, mimeType, ...}}`` → text placeholder with URI
    - ``{type: "resource_link", uri, ...}``          → text placeholder with URI
    - anything else                                  → text placeholder

    Degradations always emit ``logger.warning`` rather than raising, so a
    misbehaving server can never kill the agent loop.
    """
    max_text_bytes = config.max_result_text_bytes if config is not None else 0
    max_binary_bytes = config.max_result_binary_bytes if config is not None else 0
    blocks: list[dict[str, Any]] = []
    for block in mcp_content:
        if not isinstance(block, dict):
            _log.warning("MCP content array contained non-dict block: %r", block)
            blocks.append(
                {
                    "type": "text",
                    "text": f"[Unknown content block: {type(block).__name__}]",
                }
            )
            continue
        block_type = block.get("type")
        if block_type == "text":
            raw_text = block.get("text")
            text = raw_text if isinstance(raw_text, str) else ""
            if max_text_bytes > 0:
                text = _truncate_text(text, max_text_bytes)
            blocks.append({"type": "text", "text": text})
            continue
        if block_type == "image":
            blocks.append(_image_block_or_placeholder(block, max_binary_bytes=max_binary_bytes))
            continue
        if block_type == "audio":
            _log.warning(
                "MCP audio content block degraded to text placeholder "
                "(not supported by current providers)"
            )
            blocks.append(
                {
                    "type": "text",
                    "text": "[Audio omitted: not supported by current providers]",
                }
            )
            continue
        if block_type == "embedded_resource":
            blocks.append(_embedded_resource_placeholder(block, max_binary_bytes=max_binary_bytes))
            continue
        if block_type == "resource_link":
            uri = block.get("uri")
            uri_text = uri if isinstance(uri, str) and uri else "unknown"
            blocks.append({"type": "text", "text": f"[Resource link: {uri_text}]"})
            continue
        _log.warning("MCP content block has unknown type %r", block_type)
        blocks.append(
            {
                "type": "text",
                "text": f"[Unknown content block: {block_type or 'unknown'}]",
            }
        )
    return blocks


def _image_block_or_placeholder(
    block: dict[str, Any],
    *,
    max_binary_bytes: int = 0,
) -> dict[str, Any]:
    """Convert an MCP ``image`` content block to BareAgent's internal shape.

    Falls back to a text placeholder on any of the standard degradation paths
    (missing ``mimeType``, empty ``data``, unsupported mime). All degradations
    log at WARNING.
    """
    mime = block.get("mimeType")
    data = block.get("data")
    if not isinstance(mime, str) or not mime:
        _log.warning("MCP image block missing mimeType; degrading to placeholder")
        return {"type": "text", "text": "[Image omitted: missing mimeType]"}
    if mime not in _SUPPORTED_IMAGE_MIME_TYPES:
        _log.warning("MCP image block mime %r is not in the supported whitelist", mime)
        return {
            "type": "text",
            "text": f"[Image omitted: unsupported mime type {mime!r}]",
        }
    if not isinstance(data, str) or not data:
        _log.warning("MCP image block has empty/missing data; degrading to placeholder")
        return {"type": "text", "text": "[Image omitted: empty data]"}
    if max_binary_bytes > 0:
        # Estimate the decoded byte length from the base64 string size; avoids
        # allocating a 6 MiB bytes object just to discover we should drop it.
        estimated = _estimate_base64_bytes(data)
        if estimated > max_binary_bytes:
            _log.warning(
                "MCP image block exceeds max_result_binary_bytes (%d > %d); omitting",
                estimated,
                max_binary_bytes,
            )
            return {
                "type": "text",
                "text": _RESOURCE_OMITTED_TEMPLATE.format(n=estimated),
            }
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": data,
        },
    }


def _embedded_resource_placeholder(
    block: dict[str, Any],
    *,
    max_binary_bytes: int = 0,
) -> dict[str, Any]:
    resource = block.get("resource")
    if not isinstance(resource, dict):
        _log.warning(
            "MCP embedded_resource block missing 'resource' field; degrading to placeholder"
        )
        return {"type": "text", "text": "[Resource: unknown (unknown)]"}
    uri = resource.get("uri")
    mime = resource.get("mimeType")
    uri_text = uri if isinstance(uri, str) and uri else "unknown"
    mime_text = mime if isinstance(mime, str) and mime else "unknown"
    # If the resource was inlined as base64 ``blob`` and exceeds the binary
    # cap, surface the omission in a clear, LLM-readable form. ``text`` (no
    # blob) resources fall back to the legacy placeholder unchanged.
    blob = resource.get("blob")
    if isinstance(blob, str) and blob and max_binary_bytes > 0:
        estimated = _estimate_base64_bytes(blob)
        if estimated > max_binary_bytes:
            _log.warning(
                "MCP embedded_resource blob exceeds max_result_binary_bytes (%d > %d); omitting",
                estimated,
                max_binary_bytes,
            )
            return {
                "type": "text",
                "text": _RESOURCE_OMITTED_TEMPLATE.format(n=estimated),
            }
    return {"type": "text", "text": f"[Resource: {uri_text} ({mime_text})]"}


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
