"""Single-server MCP client: initialize handshake + tools / resources / prompts.

The client owns the JSON-RPC dialogue but not the connection: a constructed
``Transport`` is passed in by the manager so unit tests can substitute a fake.
PR3 adds resources (``resources/list`` + ``resources/read``) and prompts
(``prompts/list`` cached at handshake time + ``prompts/get`` on demand). Tools
remain lazy, as in PR2.
"""

from __future__ import annotations

import logging
import re
from threading import Lock
from typing import Any

from .config import MCPServerConfig
from .errors import MCPCallError, MCPHandshakeError, MCPProtocolError, MCPTransportError
from .protocol import Notification, Request, new_request_id
from .transport.base import Transport

_log = logging.getLogger(__name__)

# Latest MCP version BareAgent understands. Servers may negotiate down.
_CLIENT_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "BareAgent", "version": "0.1.0"}

# PRD: only ``[a-zA-Z0-9_-]`` survive — prompt names with other characters can't
# safely round-trip through the ``/mcp:<server>:<prompt>`` REPL syntax, so the
# client drops them at catalog time and warns.
_PROMPT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


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
        self._prompts: list[dict[str, Any]] | None = None
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

        # Eagerly cache the prompts catalog if the server declared the capability.
        # Failures here must not undo the handshake — log + fall back to empty.
        if self.has_capability("prompts"):
            try:
                self._prompts = self._fetch_prompts(timeout=timeout)
            except (MCPCallError, MCPProtocolError, MCPTransportError) as exc:
                _log.warning(
                    "MCP server %r prompts/list failed during start: %s",
                    self._config.name,
                    exc,
                )
                self._prompts = []

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

    def has_capability(self, name: str) -> bool:
        """Return True if the server declared the named top-level capability.

        Per MCP 2025-06-18, ``capabilities`` is a flat object whose keys (``tools``,
        ``resources``, ``prompts``, ``logging``, …) signal *presence*; sub-flags
        like ``{"prompts": {"listChanged": true}}`` are advisory. PR3 only checks
        key presence.
        """
        return name in self._server_capabilities

    def list_prompts(self) -> list[dict[str, Any]]:
        """Return the prompts catalog cached during ``start()``.

        Never re-fetches: the catalog is populated at handshake time, and if the
        server didn't declare the prompts capability the result is the empty
        list. (Prompts can change via ``notifications/prompts/list_changed`` —
        that subscription is deferred to a later PR.)
        """
        return list(self._prompts or [])

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Invoke ``prompts/get`` and return the raw result.

        The result typically contains a ``messages`` array shaped for the LLM
        ({role, content}). JSON-RPC errors raise ``MCPCallError`` so the REPL
        dispatcher can render the message verbatim; per-message field validation
        is left to the caller (the spec allows server-specific extensions).
        """
        request = Request(
            id=new_request_id(),
            method="prompts/get",
            params={"name": name, "arguments": arguments or {}},
        )
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result
        if not isinstance(result, dict):
            return {"messages": []}
        return result

    def list_resources(self, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Fetch ``resources/list`` fresh — not cached because resources are dynamic.

        Returns the raw ``resources`` array (entries typically have ``uri`` /
        ``name`` / ``description`` / ``mimeType``). Servers that omit the
        capability still get the call attempted by the registry handler; the
        handler is expected to guard the call site.
        """
        request = Request(id=new_request_id(), method="resources/list")
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result if isinstance(response.result, dict) else {}
        resources = result.get("resources")
        if not isinstance(resources, list):
            return []
        return [item for item in resources if isinstance(item, dict)]

    def read_resource(self, uri: str, *, timeout: float = 60.0) -> dict[str, Any]:
        """Invoke ``resources/read`` and return the raw result.

        ``contents`` is preserved verbatim (each block has ``type`` /
        ``text`` / ``blob`` / ``uri`` / ``mimeType`` depending on the source);
        ``isError: true`` is intentionally not raised — the registry layer
        flattens it into a ``Error: ...`` string for the LLM, mirroring the
        ``call_tool`` convention.
        """
        request = Request(
            id=new_request_id(),
            method="resources/read",
            params={"uri": uri},
        )
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result
        if not isinstance(result, dict):
            return {"contents": [], "isError": False}
        return result

    def close(self) -> None:
        """Tear down the transport. Idempotent."""
        self._safe_close()

    def is_alive(self) -> bool:
        return self._started and self._transport.is_alive()

    def _fetch_prompts(self, *, timeout: float) -> list[dict[str, Any]]:
        request = Request(id=new_request_id(), method="prompts/list")
        response = self._transport.request(request, timeout=timeout)
        if response.error is not None:
            raise MCPCallError(
                f"MCP Error: {response.error.code} {response.error.message}"
            )
        result = response.result if isinstance(response.result, dict) else {}
        prompts = result.get("prompts")
        if not isinstance(prompts, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for prompt in prompts:
            if not isinstance(prompt, dict):
                continue
            name = prompt.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not _PROMPT_NAME_RE.match(name):
                # Names outside [a-zA-Z0-9_-] would collide with the
                # ``/mcp:<server>:<prompt>`` REPL syntax. Skip + warn rather
                # than silently surface unusable entries.
                _log.warning(
                    "MCP server %r prompt %r contains characters outside "
                    "[a-zA-Z0-9_-]; skipping",
                    self._config.name,
                    name,
                )
                continue
            cleaned.append(prompt)
        return cleaned

    def _safe_close(self) -> None:
        try:
            self._transport.close()
        except Exception:
            # Closing must never raise — keeps manager shutdown loops simple.
            pass
