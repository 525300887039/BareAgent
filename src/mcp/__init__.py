"""MCP (Model Context Protocol) client subpackage.

PR1 delivers the transport + protocol scaffolding only: Client / handshake /
schema injection arrive in subsequent PRs.
"""

from __future__ import annotations

from .config import MCPConfig, MCPServerConfig, parse_mcp_config
from .errors import MCPError, MCPProtocolError, MCPTransportError
from .protocol import (
    ErrorObject,
    Notification,
    Request,
    Response,
    decode_message,
    encode_message,
    new_request_id,
)
from .transport import (
    HttpLegacyTransport,
    HttpStreamableTransport,
    StdioTransport,
    Transport,
)

__all__ = [
    "ErrorObject",
    "HttpLegacyTransport",
    "HttpStreamableTransport",
    "MCPConfig",
    "MCPError",
    "MCPProtocolError",
    "MCPServerConfig",
    "MCPTransportError",
    "Notification",
    "Request",
    "Response",
    "StdioTransport",
    "Transport",
    "decode_message",
    "encode_message",
    "new_request_id",
    "parse_mcp_config",
]
