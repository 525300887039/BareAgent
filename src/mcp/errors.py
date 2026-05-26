"""MCP error hierarchy.

Only the lowest-level errors live here. PR2 will add MCPHandshakeError /
MCPCallError once the client / lifecycle code exists.
"""

from __future__ import annotations


class MCPError(Exception):
    """Base class for all MCP-related failures."""


class MCPTransportError(MCPError):
    """Transport-layer failure: connection dropped, framing error, subprocess died."""


class MCPProtocolError(MCPError):
    """JSON-RPC protocol failure: timeout, unknown response id, malformed envelope."""
