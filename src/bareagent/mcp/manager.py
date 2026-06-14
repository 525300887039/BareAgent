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
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Any

from .client import MCPClient
from .config import MCPConfig, MCPServerConfig
from .errors import MCPError
from .transport.base import Transport
from .transport.http_legacy import HttpLegacyTransport
from .transport.http_streamable import HttpStreamableTransport
from .transport.stdio import StdioTransport

if TYPE_CHECKING:
    from bareagent.concurrency.background import BackgroundManager
    from bareagent.ui.protocol import UIProtocol

_log = logging.getLogger(__name__)


class ServerStatus(StrEnum):
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
        notifier: BackgroundManager | None = None,
    ) -> None:
        self._config = config
        self._console = console
        # ``notifier`` is the shared ``BackgroundManager`` already used for
        # background-task completion notifications. When a managed MCP server
        # disconnects unexpectedly, the manager posts a "failed" notification
        # through the same channel so the REPL surface treats it as an async
        # event (see ``concurrency/notification.py``).
        self._notifier = notifier
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
            client = self._build_client(server)
        except Exception as exc:
            _log.warning(
                "MCP transport construction failed for %r: %s", server.name, exc
            )
            with self._lock:
                self._status[server.name] = ServerStatus.UNHEALTHY
            self._warn(f"MCP server {server.name!r} transport setup failed: {exc}")
            return

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

    def _build_client(self, server: MCPServerConfig) -> MCPClient:
        """Construct a transport + MCPClient for ``server``.

        Extracted from ``_start_one`` so ``reload`` can rebuild a server using
        the exact same wiring. Raises whatever the transport / config layer
        raises — the caller decides how to mark the server.
        """
        transport = self._construct_transport(server)
        # Register the proactive disconnect hook so the manager learns about
        # subprocess death / SSE stream loss the moment the reader thread sees
        # it — without waiting for the next call to surface the failure.
        transport.set_disconnect_handler(
            lambda reason, _name=server.name: self._on_disconnect(_name, reason)
        )
        return MCPClient(server, transport)

    def _on_disconnect(self, name: str, reason: str) -> None:
        """Mark a server unhealthy and surface the event to the user immediately.

        Called by transport reader threads on unexpected disconnect (EOF,
        broken pipe, SSE stream break). Idempotent: if the server is already
        non-RUNNING, the console / notifier still fire so the user always sees
        the message at least once per real failure.
        """
        with self._lock:
            self._status[name] = ServerStatus.UNHEALTHY
            self._clients.pop(name, None)
        message = f"MCP server {name!r} disconnected: {reason}"
        if self._console is not None:
            try:
                self._console.print_error(message)
            except Exception:  # pragma: no cover — console must never crash reader
                pass
        if self._notifier is not None:
            try:
                self._notifier.notify(f"mcp:{name}", message)
            except Exception:  # pragma: no cover — notification must never crash
                pass

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

    def reload(self, name: str) -> None:
        """Tear down ``name`` and rebuild it from the current config entry.

        Failure path follows the fleet-wide convention: the old client is
        dropped, status becomes ``UNHEALTHY``, and the exception is re-raised
        so the REPL handler can render a message. The config file is NOT
        re-read — config hot-reload is intentionally out of scope for v1.
        """
        server_cfg = next(
            (s for s in self._config.servers if s.name == name),
            None,
        )
        if server_cfg is None:
            raise MCPError(f"MCP server {name!r} is not in config")

        with self._lock:
            old_client = self._clients.pop(name, None)
            self._status[name] = ServerStatus.STARTING

        if old_client is not None:
            try:
                old_client.close()
            except Exception as exc:  # pragma: no cover — close is idempotent
                _log.warning(
                    "MCP server %r old client close failed during reload: %s",
                    name,
                    exc,
                )

        try:
            new_client = self._build_client(server_cfg)
            new_client.start(timeout=server_cfg.start_timeout)
        except Exception as exc:
            _log.warning("MCP server %r reload failed: %s", name, exc)
            with self._lock:
                self._status[name] = ServerStatus.UNHEALTHY
            raise

        with self._lock:
            self._clients[name] = new_client
            self._status[name] = ServerStatus.RUNNING

    def summarize(self) -> list[dict[str, Any]]:
        """Return a per-server status dict for the ``/mcp status`` REPL command.

        Server order follows ``config.servers`` (insertion order in TOML), not
        the internal ``_clients`` dict — that way the listing stays stable
        across reloads. Tool / prompt counts read the client caches directly;
        they are zero for non-running servers.
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            for server in self._config.servers:
                name = server.name
                status = self._status.get(name, ServerStatus.STOPPED)
                client = self._clients.get(name)
                is_running = status == ServerStatus.RUNNING and client is not None
                tool_count = 0
                prompt_count = 0
                has_resources = False
                if is_running and client is not None:
                    cached_tools = getattr(client, "_tools_cache", None)
                    if isinstance(cached_tools, list):
                        tool_count = len(cached_tools)
                    cached_prompts = getattr(client, "_prompts", None)
                    if isinstance(cached_prompts, list):
                        prompt_count = len(cached_prompts)
                    has_resources = client.has_capability("resources")
                out.append(
                    {
                        "name": name,
                        "status": status.value,
                        "tool_count": tool_count,
                        "has_resources": has_resources,
                        "prompt_count": prompt_count,
                    }
                )
        return out

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
