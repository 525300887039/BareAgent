"""Single-server MCP client: initialize handshake, tools/list, tools/call.

The client owns the JSON-RPC dialogue but not the connection: a constructed
``Transport`` is passed in by the manager so unit tests can substitute a fake.
PR2 implements only the tools capability path; resources / prompts / sampling
are deferred to later PRs.
"""

from __future__ import annotations

from threading import Lock
from typing import Any

from .config import MCPServerConfig
from .errors import MCPCallError, MCPHandshakeError, MCPProtocolError, MCPTransportError
from .protocol import Notification, Request, new_request_id
from .transport.base import Transport

# Latest MCP version BareAgent understands. Servers may negotiate down.
_CLIENT_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "BareAgent", "version": "0.1.0"}


class MCPClient:
    """One MCP server connection.

    Lifecycle: ``start()`` runs the initialize handshake; ``list_tools()`` and
    ``call_tool()`` are the operational surface; ``close()`` shuts down the
    transport. Methods are threadsafe — the underlying transport already
    serializes writes, and the tool cache is guarded by a local lock.
    """

    def __init__(self, config: MCPServerConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._cache_lock = Lock()
        self._tools_cache: list[dict[str, Any]] | None = None
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._negotiated_version: str | None = None
        self._started = False

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return dict(self._server_capabilities)

    def start(self, timeout: float) -> None:
        """Open the transport and run the initialize handshake.

        Raises ``MCPHandshakeError`` on timeout, JSON-RPC error, or any
        transport-level failure during handshake. Successful return guarantees
        the server has acknowledged ``notifications/initialized``.
        """
        if self._started:
            raise MCPHandshakeError(f"client {self._config.name!r} already started")
        try:
            self._transport.start()
        except MCPTransportError as exc:
            raise MCPHandshakeError(f"transport start failed: {exc}") from exc

        init_request = Request(
            id=new_request_id(),
            method="initialize",
            params={
                "protocolVersion": _CLIENT_PROTOCOL_VERSION,
                "capabilities": {},  # PR2: client offers no capabilities
                "clientInfo": _CLIENT_INFO,
            },
        )
        try:
            response = self._transport.request(init_request, timeout=timeout)
        except (MCPTransportError, MCPProtocolError) as exc:
            self._safe_close()
            raise MCPHandshakeError(f"initialize failed: {exc}") from exc

        if response.error is not None:
            self._safe_close()
            raise MCPHandshakeError(
                f"initialize returned error: {response.error.code} {response.error.message}"
            )

        result = response.result if isinstance(response.result, dict) else {}
        self._negotiated_version = result.get("protocolVersion")
        info = result.get("serverInfo")
        if isinstance(info, dict):
            self._server_info = info
        caps = result.get("capabilities")
        if isinstance(caps, dict):
            self._server_capabilities = caps

        try:
            self._transport.notify(Notification(method="notifications/initialized"))
        except MCPTransportError as exc:
            self._safe_close()
            raise MCPHandshakeError(
                f"failed to send initialized notification: {exc}"
            ) from exc

        self._started = True

    def list_tools(self, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Return cached or freshly fetched ``tools/list`` entries.

        Each entry preserves the raw ``name`` / ``description`` / ``inputSchema``
        from the server (the registry layer adds the ``mcp__<server>__`` prefix
        when assembling BareAgent schemas).
        """
        with self._cache_lock:
            if self._tools_cache is not None:
                return list(self._tools_cache)

        if "tools" not in self._server_capabilities:
            # Server didn't declare tools capability — skip the call, cache empty.
            # Note: an empty dict ``{}`` still means "supported, no sub-capabilities",
            # so presence (not truthiness) is the right check.
            with self._cache_lock:
                self._tools_cache = []
                return []

        request = Request(id=new_request_id(), method="tools/list")
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result if isinstance(response.result, dict) else {}
        tools = result.get("tools")
        if not isinstance(tools, list):
            tools = []
        # Filter out anything missing the required fields.
        cleaned: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            cleaned.append(tool)
        with self._cache_lock:
            self._tools_cache = cleaned
        return list(cleaned)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Invoke a tool on the server and return the raw ``result`` object.

        ``isError: true`` is intentionally NOT raised — the registry layer
        formats it into a plain ``Error: ...`` string so the LLM sees the
        failure as data and can retry. JSON-RPC protocol errors do raise
        (``MCPCallError`` with the ``MCP Error: <code> <message>`` prefix).
        """
        request = Request(
            id=new_request_id(),
            method="tools/call",
            params={"name": name, "arguments": arguments or {}},
        )
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result
        if not isinstance(result, dict):
            return {"content": [], "isError": False}
        return result

    def close(self) -> None:
        """Tear down the transport. Idempotent."""
        self._safe_close()

    def is_alive(self) -> bool:
        return self._started and self._transport.is_alive()

    def _safe_close(self) -> None:
        try:
            self._transport.close()
        except Exception:
            # Closing must never raise — keeps manager shutdown loops simple.
            pass
