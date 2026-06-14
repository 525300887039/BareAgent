"""MCP (Model Context Protocol) client subpackage.

PR1 delivered the transport + protocol scaffolding. PR2 adds the client
lifecycle (``MCPClient``), multi-server orchestration (``MCPManager``), and
the BareAgent tool registry shims (``build_mcp_tool_schemas`` /
``build_mcp_handlers``). Resources / prompts / multimodal passthrough /
REPL plumbing / atexit cleanup arrive in subsequent PRs.
"""

from __future__ import annotations

from .client import MCPClient
from .config import MCPConfig, MCPServerConfig, parse_mcp_config
from .errors import (
    MCPCallError,
    MCPError,
    MCPHandshakeError,
    MCPProtocolError,
    MCPTransportError,
)
from .manager import MCPManager, ServerStatus
from .protocol import (
    ErrorObject,
    Notification,
    Request,
    Response,
    decode_message,
    encode_message,
    new_request_id,
)
from .registry import (
    build_mcp_handlers,
    build_mcp_tool_schemas,
    mcp_tool_name,
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
    "MCPCallError",
    "MCPClient",
    "MCPConfig",
    "MCPError",
    "MCPHandshakeError",
    "MCPManager",
    "MCPProtocolError",
    "MCPServerConfig",
    "MCPTransportError",
    "Notification",
    "Request",
    "Response",
    "ServerStatus",
    "StdioTransport",
    "Transport",
    "build_mcp_handlers",
    "build_mcp_tool_schemas",
    "decode_message",
    "encode_message",
    "mcp_tool_name",
    "new_request_id",
    "parse_mcp_config",
]
