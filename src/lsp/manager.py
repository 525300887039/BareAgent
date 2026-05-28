"""Multi-server LSP manager: concurrent startup, status tracking, file routing.

Wraps multilspy's ``SyncLanguageServer`` (one instance per language) behind a
single facade. The manager constructs every configured server in parallel,
enters each ``start_server()`` context, and stores the live server alongside
its lifecycle status. Slow / failed servers cannot block REPL boot — handshake
exceptions and timeouts mark that language ``UNHEALTHY`` while the rest
continue.

multilspy is an optional dependency. When it isn't installed, ``start_all()``
runs as a no-op and every configured server is marked ``UNHEALTHY`` with the
reason ``"multilspy extra not installed"``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from .config import LSPConfig, LSPServerConfig

if TYPE_CHECKING:
    from src.ui.protocol import UIProtocol

_log = logging.getLogger(__name__)


class ServerStatus(str, Enum):
    """Lifecycle states a managed language server moves through."""

    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


# Sentinel used by ``_status_reasons`` for servers that have a status but no
# attached reason string yet (e.g. the still-starting / cleanly stopped paths).
_NO_REASON = ""


class _ServerEntry:
    """Internal bookkeeping for one language server.

    Holds the live multilspy instance plus the bookkeeping needed to shut it
    down cleanly. ``stop_event`` and ``thread`` exist because multilspy's
    ``start_server`` is a context manager — we keep a worker thread alive
    inside that ``with`` block so the loop and the language server process
    stay running between calls.
    """

    __slots__ = (
        "server",
        "thread",
        "started_event",
        "stop_event",
        "exit_error",
    )

    def __init__(self) -> None:
        self.server: Any = None  # the entered SyncLanguageServer (post-__enter__)
        self.thread: threading.Thread | None = None
        self.started_event = threading.Event()
        self.stop_event = threading.Event()
        self.exit_error: BaseException | None = None


class LanguageServerManager:
    """Orchestrates a fleet of multilspy language servers.

    Use ``start_all()`` once at boot, then ``get_server_for_file(path)`` /
    ``iter_running()`` to consume the live instances. Failed servers are
    skipped (logged + surfaced via the UI console if supplied) so REPL
    startup is never blocked.
    """

    def __init__(
        self,
        config: LSPConfig,
        console: UIProtocol | None = None,
        repository_root: str | None = None,
    ) -> None:
        self._config = config
        self._console = console
        self._repository_root = (
            os.path.abspath(repository_root) if repository_root else os.getcwd()
        )
        self._lock = threading.Lock()
        # Status of every configured server, keyed by language. Populated
        # eagerly in ``start_all`` so callers can ``get_status`` even before
        # the first server finishes its handshake.
        self._status: dict[str, ServerStatus] = {}
        self._status_reasons: dict[str, str] = {}
        self._entries: dict[str, _ServerEntry] = {}
        # Extension → language lookup, built once from the config so file
        # routing is O(1). Lowercased / leading-dot to match the config
        # parser's normalization.
        self._ext_to_language: dict[str, str] = {}
        for server in config.servers:
            for ext in server.extensions:
                self._ext_to_language[ext] = server.language
        self._on_disconnect: Callable[[str, str], None] | None = None

    # ------------------------------------------------------------------ API

    @property
    def config(self) -> LSPConfig:
        return self._config

    @property
    def repository_root(self) -> str:
        return self._repository_root

    def set_on_disconnect(self, callback: Callable[[str, str], None] | None) -> None:
        """Register a callback for unexpected server disconnects.

        ``callback(language, reason)`` will be invoked once per real failure.
        Reserved for child B (notifier + console wiring); the manager keeps
        the slot here so callers can install the hook today.
        """
        self._on_disconnect = callback

    def start_all(self) -> None:
        """Spawn every configured server in parallel; never raises.

        Each ``SyncLanguageServer.create(...).start_server()`` runs in its own
        worker thread. Failures and timeouts are caught and recorded — the
        method returns when every server has either reached ``RUNNING`` or
        been marked ``UNHEALTHY``.
        """
        servers = list(self._config.servers)
        if not servers:
            return

        with self._lock:
            for server in servers:
                self._status[server.language] = ServerStatus.STARTING
                self._status_reasons[server.language] = _NO_REASON

        sync_cls = _import_sync_language_server()
        if sync_cls is None:
            reason = "multilspy extra not installed"
            with self._lock:
                for server in servers:
                    self._status[server.language] = ServerStatus.UNHEALTHY
                    self._status_reasons[server.language] = reason
            self._warn(
                "LSP servers disabled: multilspy is not installed. "
                'Install with `uv pip install -e ".[lsp]"`.'
            )
            return

        max_workers = max(1, len(servers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._start_one, server, sync_cls): server
                for server in servers
            }
            for future in as_completed(futures):
                server = futures[future]
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover — defensive net
                    _log.warning(
                        "LSP server %r start crashed unexpectedly: %s",
                        server.language,
                        exc,
                    )
                    with self._lock:
                        self._status[server.language] = ServerStatus.UNHEALTHY
                        self._status_reasons[server.language] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                    self._warn(f"LSP server {server.language!r} failed to start: {exc}")

    def get_server_for_file(self, path: str) -> Any | None:
        """Return the running ``SyncLanguageServer`` whose extension matches
        ``path``. Returns ``None`` when no extension matches or when the
        matched server is not currently RUNNING.
        """
        language = self.language_for_file(path)
        if language is None:
            return None
        with self._lock:
            if self._status.get(language) != ServerStatus.RUNNING:
                return None
            entry = self._entries.get(language)
        return entry.server if entry is not None else None

    def language_for_file(self, path: str) -> str | None:
        """Map a file path to its configured language, or None if no
        ``[[lsp.servers]]`` entry claims the extension."""
        _, ext = os.path.splitext(path)
        if not ext:
            return None
        return self._ext_to_language.get(ext.lower())

    def get_status(self, language: str) -> ServerStatus | None:
        """Lifecycle status for ``language``, or ``None`` when the language is
        not in the active config."""
        with self._lock:
            return self._status.get(language)

    def get_status_reason(self, language: str) -> str:
        """Free-form explanation for the current status (e.g. handshake error
        message). Empty string when no reason is recorded."""
        with self._lock:
            return self._status_reasons.get(language, _NO_REASON)

    def iter_running(self) -> Iterator[tuple[str, Any]]:
        """Yield ``(language, SyncLanguageServer)`` pairs for RUNNING servers.

        Snapshot is taken under the lock so iteration is safe even if a
        teardown is in flight on another thread.
        """
        with self._lock:
            snapshot = [
                (language, entry.server)
                for language, entry in self._entries.items()
                if self._status.get(language) == ServerStatus.RUNNING
            ]
        yield from snapshot

    def reload(self, language: str) -> None:
        """Tear down ``language`` and rebuild it from the active config entry.

        Reserved for child B (REPL ``/lsp reload`` command). Behaviour mirrors
        ``MCPManager.reload``: the old instance is stopped, status moves to
        ``STARTING``, then the handshake is retried. On failure status ends
        up ``UNHEALTHY`` and the exception is re-raised so the caller can
        render a message.
        """
        server_cfg = next(
            (s for s in self._config.servers if s.language == language),
            None,
        )
        if server_cfg is None:
            from .errors import LSPError

            raise LSPError(f"LSP server {language!r} is not in config")

        self._stop_one(language)

        sync_cls = _import_sync_language_server()
        if sync_cls is None:
            reason = "multilspy extra not installed"
            with self._lock:
                self._status[language] = ServerStatus.UNHEALTHY
                self._status_reasons[language] = reason
            from .errors import LSPHandshakeError

            raise LSPHandshakeError(reason)

        with self._lock:
            self._status[language] = ServerStatus.STARTING
            self._status_reasons[language] = _NO_REASON
        self._start_one(server_cfg, sync_cls)
        if self._status.get(language) != ServerStatus.RUNNING:
            from .errors import LSPHandshakeError

            raise LSPHandshakeError(
                self.get_status_reason(language) or "handshake failed"
            )

    def close_all(self) -> None:
        """Stop every managed server. Idempotent; safe to call on exit."""
        with self._lock:
            languages = list(self._entries.keys())
        for language in languages:
            self._stop_one(language)
        with self._lock:
            for language in self._status:
                if self._status[language] != ServerStatus.UNHEALTHY:
                    self._status[language] = ServerStatus.STOPPED

    # ----------------------------------------------------------- internals

    def _start_one(self, server: LSPServerConfig, sync_cls: Any) -> None:
        """Spawn one language server and wait for handshake or timeout.

        Runs the multilspy ``start_server()`` context manager inside a daemon
        thread so the language-server subprocess stays alive between LSP
        tool calls. Marks the language ``RUNNING`` once ``__enter__`` returns,
        otherwise records ``UNHEALTHY`` + a reason.
        """
        entry = _ServerEntry()
        with self._lock:
            self._entries[server.language] = entry

        thread = threading.Thread(
            target=self._run_server_lifecycle,
            args=(server, entry, sync_cls),
            name=f"lsp-{server.language}",
            daemon=True,
        )
        entry.thread = thread
        thread.start()

        ready = entry.started_event.wait(timeout=self._config.start_timeout)
        if not ready:
            with self._lock:
                self._status[server.language] = ServerStatus.UNHEALTHY
                self._status_reasons[server.language] = (
                    f"handshake timed out after {self._config.start_timeout}s"
                )
            self._warn(
                f"LSP server {server.language!r} timed out after "
                f"{self._config.start_timeout}s"
            )
            return

        if entry.exit_error is not None:
            error = entry.exit_error
            with self._lock:
                self._status[server.language] = ServerStatus.UNHEALTHY
                self._status_reasons[server.language] = (
                    f"{type(error).__name__}: {error}"
                )
            self._warn(f"LSP server {server.language!r} unhealthy: {error}")
            return

        if entry.server is None:
            with self._lock:
                self._status[server.language] = ServerStatus.UNHEALTHY
                self._status_reasons[server.language] = "handshake produced no server"
            self._warn(f"LSP server {server.language!r} unhealthy: empty server")
            return

        with self._lock:
            self._status[server.language] = ServerStatus.RUNNING
            self._status_reasons[server.language] = _NO_REASON

    def _run_server_lifecycle(
        self,
        server: LSPServerConfig,
        entry: _ServerEntry,
        sync_cls: Any,
    ) -> None:
        """Daemon body: build the server, enter its ``start_server()`` context,
        then block on ``stop_event`` until ``close_all`` / ``reload`` signal
        teardown. Any exception aborts the wait and is reported back through
        ``entry.exit_error``.
        """
        try:
            sync_server = _build_sync_server(sync_cls, server, self._repository_root)
        except Exception as exc:
            entry.exit_error = exc
            entry.started_event.set()
            return

        ctx = sync_server.start_server()
        try:
            entered = ctx.__enter__()
        except Exception as exc:
            entry.exit_error = exc
            entry.started_event.set()
            return

        entry.server = entered
        entry.started_event.set()

        try:
            # Block here until ``stop_event`` is set. This keeps the multilspy
            # event-loop thread (which it spawns inside start_server) alive
            # so subsequent ``request_*`` calls work.
            entry.stop_event.wait()
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover — best-effort shutdown
                entry.exit_error = exc
                _log.warning("LSP server %r exit raised: %s", server.language, exc)

    def _stop_one(self, language: str) -> None:
        """Signal one server's lifecycle thread to tear down and wait briefly
        for it. ``stop_event`` is the only synchronization needed —
        ``start_server`` is a context manager that handles its own cleanup."""
        with self._lock:
            entry = self._entries.pop(language, None)
        if entry is None:
            return
        entry.stop_event.set()
        thread = entry.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _warn(self, message: str) -> None:
        if self._console is None:
            return
        try:
            self._console.print_error(message)
        except Exception:  # pragma: no cover — console must never break boot
            pass


def _import_sync_language_server() -> Any | None:
    """Lazy import of ``multilspy.SyncLanguageServer``. Returns ``None`` when
    the extra is not installed so callers can degrade gracefully."""
    try:
        from multilspy import SyncLanguageServer  # type: ignore
    except ImportError:
        return None
    return SyncLanguageServer


def _build_sync_server(
    sync_cls: Any,
    server: LSPServerConfig,
    repository_root: str,
) -> Any:
    """Construct a ``SyncLanguageServer`` for ``server``.

    Translates :class:`LSPServerConfig` into the multilspy types
    (``MultilspyConfig`` + ``MultilspyLogger``) without exposing multilspy
    to callers.
    """
    from multilspy.multilspy_config import MultilspyConfig  # type: ignore
    from multilspy.multilspy_logger import MultilspyLogger  # type: ignore

    config = MultilspyConfig.from_dict({"code_language": server.language})
    logger = MultilspyLogger()
    return sync_cls.create(config, logger, repository_root)
