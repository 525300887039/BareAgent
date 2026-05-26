"""MCP error hierarchy.

Layered failure types: transport (subprocess / socket), protocol (JSON-RPC
framing / id routing), handshake (initialize lifecycle), and call (tools/call
returning a JSON-RPC error). Tool execution errors (``result.isError: true``)
are NOT exceptions — they flow back to the LLM as text via the registry layer.
"""

from __future__ import annotations


class MCPError(Exception):
    """Base class for all MCP-related failures."""


class MCPTransportError(MCPError):
    """Transport-layer failure: connection dropped, framing error, subprocess died."""


class MCPProtocolError(MCPError):
    """JSON-RPC protocol failure: timeout, unknown response id, malformed envelope."""


class MCPHandshakeError(MCPError):
    """Initialize lifecycle failed: timeout, server returned error, or
    incompatible protocol version negotiation."""


class MCPCallError(MCPError):
    """``tools/call`` (or any other request) came back with a JSON-RPC error
    object. The exception message is pre-formatted as ``MCP Error: <code> <message>``
    so registry handlers can return ``str(exc)`` directly to the LLM."""
