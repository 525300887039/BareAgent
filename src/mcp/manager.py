"""Multi-server MCP manager: concurrent startup, status tracking, lookup.

The manager constructs one ``Transport`` + one ``MCPClient`` per configured
server, then launches every handshake in parallel through a thread pool. A
single slow server cannot block REPL boot — handshake timeouts and exceptions
mark that server unhealthy while the rest continue.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from threading import Lock
from typing import TYPE_CHECKING

from .client import MCPClient
from .config import MCPConfig, MCPServerConfig
from .errors import MCPError
from .transport.base import Transport
from .transport.http_legacy import HttpLegacyTransport
from .transport.http_streamable import HttpStreamableTransport
from .transport.stdio import StdioTransport

if TYPE_CHECKING:
    from src.ui.protocol import UIProtocol

_log = logging.getLogger(__name__)


class ServerStatus(str, Enum):
    """Lifecycle states a managed MCP server moves through."""

    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


class MCPManager:
    """Orchestrates a fleet of MCP server clients.

    Use ``start_all()`` once at boot, then ``iter_running_clients()`` to feed
    the tool registry. Failed servers are skipped (logged + warned via the
    UI console if supplied) so REPL startup is never blocked.
    """

    def __init__(
        self,
        config: MCPConfig,
        console: UIProtocol | None = None,
    ) -> None:
        self._config = config
        self._console = console
        self._clients: dict[str, MCPClient] = {}
        self._status: dict[str, ServerStatus] = {}
        self._lock = Lock()

    @property
    def config(self) -> MCPConfig:
        return self._config

    def start_all(self) -> None:
        """Spawn every configured server in parallel; never raises.

        Each handshake runs in its own worker thread; failures are caught and
        recorded. The call returns when every server has either reached
        ``RUNNING`` or been marked ``UNHEALTHY``.
        """
        servers = list(self._config.servers)
        if not servers:
            return

        with self._lock:
            for server in servers:
                self._status[server.name] = ServerStatus.STARTING

        max_workers = max(1, len(servers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._start_one, server): server for server in servers
            }
            for future in as_completed(futures):
                server = futures[future]
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover — defensive net
                    _log.warning(
                        "MCP server %r start crashed unexpectedly: %s",
                        server.name,
                        exc,
                    )
                    with self._lock:
                        self._status[server.name] = ServerStatus.UNHEALTHY
                    self._warn(f"MCP server {server.name!r} failed to start: {exc}")

    def _start_one(self, server: MCPServerConfig) -> None:
        try:
            transport = self._construct_transport(server)
        except Exception as exc:
            _log.warning(
                "MCP transport construction failed for %r: %s", server.name, exc
            )
            with self._lock:
                self._status[server.name] = ServerStatus.UNHEALTHY
            self._warn(f"MCP server {server.name!r} transport setup failed: {exc}")
            return

        client = MCPClient(server, transport)
        try:
            client.start(timeout=server.start_timeout)
        except Exception as exc:
            _log.warning("MCP server %r handshake failed: %s", server.name, exc)
            with self._lock:
                self._status[server.name] = ServerStatus.UNHEALTHY
            self._warn(f"MCP server {server.name!r} unhealthy: {exc}")
            return

        with self._lock:
            self._clients[server.name] = client
            self._status[server.name] = ServerStatus.RUNNING

    def get_client(self, name: str) -> MCPClient | None:
        """Return the running client for ``name`` or ``None`` if it isn't healthy."""
        with self._lock:
            status = self._status.get(name)
            if status != ServerStatus.RUNNING:
                return None
            return self._clients.get(name)

    def get_status(self, name: str) -> ServerStatus | None:
        with self._lock:
            return self._status.get(name)

    def iter_running_clients(self) -> Iterator[tuple[str, MCPClient]]:
        """Yield ``(name, client)`` pairs only for servers currently RUNNING."""
        with self._lock:
            snapshot = [
                (name, client)
                for name, client in self._clients.items()
                if self._status.get(name) == ServerStatus.RUNNING
            ]
        yield from snapshot

    def close_all(self) -> None:
        """Tear down every managed client. Idempotent; safe to call on exit."""
        with self._lock:
            clients = list(self._clients.items())
            self._clients.clear()
        for name, client in clients:
            try:
                client.close()
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("MCP server %r close failed: %s", name, exc)
            with self._lock:
                self._status[name] = ServerStatus.STOPPED

    def _construct_transport(self, server: MCPServerConfig) -> Transport:
        if server.transport == "stdio":
            command = list(server.command) + list(server.args)
            return StdioTransport(command, env=server.env or None, cwd=server.cwd)
        if server.transport == "http_legacy":
            if not server.url:
                raise MCPError(
                    f"mcp.servers[{server.name}].url required for http_legacy"
                )
            return HttpLegacyTransport(
                server.url,
                headers=server.headers,
                start_timeout=server.start_timeout,
            )
        if server.transport == "http_streamable":
            if not server.url:
                raise MCPError(
                    f"mcp.servers[{server.name}].url required for http_streamable"
                )
            return HttpStreamableTransport(
                server.url,
                headers=server.headers,
                start_timeout=server.start_timeout,
            )
        raise MCPError(
            f"mcp.servers[{server.name}].transport unsupported: {server.transport!r}"
        )

    def _warn(self, message: str) -> None:
        if self._console is None:
            return
        try:
            self._console.print_error(message)
        except Exception:  # pragma: no cover — console must never break boot
            pass
