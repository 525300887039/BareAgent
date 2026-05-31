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

Diagnostics surface
-------------------
multilspy 0.0.15 explicitly registers a ``do_nothing`` handler for the
``textDocument/publishDiagnostics`` notification on every bundled language-
server adapter (see ``multilspy/language_servers/<lang>/<lang>.py``). It also
does not expose pull-diagnostics on ``SyncLanguageServer``. The manager works
around both gaps post-handshake by:

1. Reaching into ``sync_server.language_server.server.on_notification_handlers``
   (the underlying ``LanguageServerHandler`` dict) and replacing the bundled
   ``do_nothing`` with our own callback. This is monkey-patching, but the
   surface is stable across multilspy patch releases and matches the only
   route through which pyright/etc. publish diagnostics.
2. Maintaining a per-server cache keyed by *relative path* with a
   ``threading.Event`` that fires whenever a new payload arrives. The Hybrid
   auto-diagnostics hook in ``src/core/handlers/file_edit.py`` waits on this
   event briefly to bridge pyright's incremental analysis lag.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from .config import LSPConfig, LSPServerConfig
from .coord import path_to_document_uri, to_repo_relative

if TYPE_CHECKING:
    from src.concurrency.background import BackgroundManager
    from src.ui.protocol import UIProtocol

_log = logging.getLogger(__name__)


class ServerStatus(StrEnum):
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
        "diagnostics",
        "diagnostics_lock",
        "diagnostics_events",
        "disconnect_seen",
    )

    def __init__(self) -> None:
        self.server: Any = None  # the entered SyncLanguageServer (post-__enter__)
        self.thread: threading.Thread | None = None
        self.started_event = threading.Event()
        self.stop_event = threading.Event()
        self.exit_error: BaseException | None = None
        # Push-diagnostics cache keyed by *relative path* (the same form the
        # tool handlers pass to multilspy). Each entry is the latest list of
        # raw LSP Diagnostic dicts seen via ``textDocument/publishDiagnostics``.
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostics_lock = threading.Lock()
        # Per-file Event so callers can ``wait_for_diagnostics(file)`` until
        # the server publishes the next analysis pass. Lazily created on
        # first miss so we don't allocate one per file in the workspace.
        self.diagnostics_events: dict[str, threading.Event] = {}
        # Tripped by ``_on_disconnect`` so we only emit the failure notice
        # once per server crash (the watchdog poller could fire repeatedly).
        self.disconnect_seen = False


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
        notifier: BackgroundManager | None = None,
    ) -> None:
        self._config = config
        self._console = console
        # ``notifier`` is the shared ``BackgroundManager`` used for background-
        # task completion notifications. Disconnect events ride the same
        # channel so the REPL surface treats them as async events (same
        # pattern as ``MCPManager``).
        self._notifier = notifier
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
        # External callback slot — installed via :meth:`set_on_disconnect`.
        # Kept under a distinct attribute name from the internal
        # ``_on_disconnect`` method so the latter is reachable directly
        # from the watchdog (and from tests synthesising crashes) even
        # when a user callback is registered.
        self._on_disconnect_callback: Callable[[str, str], None] | None = None
        # Subprocess crash watchdog. One daemon thread polls
        # ``language_server.server.process.returncode`` on a short interval
        # and reports unexpected exits through ``_on_disconnect``. Lazily
        # started after the first successful handshake.
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    # ------------------------------------------------------------------ API

    @property
    def config(self) -> LSPConfig:
        return self._config

    @property
    def repository_root(self) -> str:
        return self._repository_root

    def set_on_disconnect(self, callback: Callable[[str, str], None] | None) -> None:
        """Register an extra callback for unexpected server disconnects.

        ``callback(language, reason)`` is invoked **in addition to** the
        built-in console + notifier path (see :meth:`_on_disconnect`). Pass
        ``None`` to clear. Reserved for tests / extensions; production callers
        normally just pass ``notifier=...`` at construction.
        """
        self._on_disconnect_callback = callback

    def get_diagnostics_snapshot(self, file_path: str) -> list[dict[str, Any]]:
        """Return the latest cached publishDiagnostics rows for ``file_path``.

        Resolves the language by extension, then looks up the cache built by
        the ``textDocument/publishDiagnostics`` handler we installed during
        the handshake (see :meth:`_install_diagnostics_handler`). Returns an
        empty list when no language routes the file or no rows have been
        published yet.
        """
        language = self.language_for_file(file_path)
        if language is None:
            return []
        abs_path = file_path if os.path.isabs(file_path) else os.path.abspath(file_path)
        rel = to_repo_relative(abs_path, self._repository_root)
        with self._lock:
            entry = self._entries.get(language)
        if entry is None:
            return []
        # Try several common key shapes — multilspy normalises URIs but
        # pyright sometimes echoes the absolute path verbatim, and Windows
        # paths use ``\`` vs ``/`` interchangeably.
        keys = (
            rel,
            rel.replace("\\", "/"),
            abs_path,
            abs_path.replace("\\", "/"),
        )
        with entry.diagnostics_lock:
            for key in keys:
                rows = entry.diagnostics.get(key)
                if rows is not None:
                    return list(rows)
        return []

    def wait_for_diagnostics(self, file_path: str, timeout: float = 1.5) -> bool:
        """Block up to ``timeout`` seconds for the next publishDiagnostics on
        ``file_path``. Returns True if a publish happened, False on timeout.

        Used by the Hybrid auto-diagnostics hook to bridge pyright's
        incremental analysis lag — a write_file landing means the LSP server
        needs a moment to re-analyse before its next ``publishDiagnostics``
        reflects the new content. Modeled after Serena's ``analysis_complete``
        Event pattern (see PRD ``Technical Approach``).
        """
        language = self.language_for_file(file_path)
        if language is None:
            return False
        abs_path = file_path if os.path.isabs(file_path) else os.path.abspath(file_path)
        rel = to_repo_relative(abs_path, self._repository_root)
        with self._lock:
            entry = self._entries.get(language)
        if entry is None:
            return False
        with entry.diagnostics_lock:
            event = entry.diagnostics_events.get(rel)
            if event is None:
                event = threading.Event()
                entry.diagnostics_events[rel] = event
            event.clear()
        return event.wait(timeout=timeout)

    def summarize(self) -> list[dict[str, Any]]:
        """Return a per-server summary for the ``/lsp status`` REPL command.

        Order follows ``config.servers`` (TOML insertion order) so the
        listing is stable across reloads. Tool count is ``4`` (Tier-1 tools)
        only for currently RUNNING servers; UNHEALTHY / STOPPED servers
        report ``0`` so the user can see at a glance whether the server is
        actually contributing to ``get_tools()``.
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            for server_cfg in self._config.servers:
                language = server_cfg.language
                status = self._status.get(language, ServerStatus.STOPPED)
                running = status == ServerStatus.RUNNING
                out.append(
                    {
                        "language": language,
                        "status": status.value,
                        "tool_count": 4 if running else 0,
                        "extensions": list(server_cfg.extensions),
                        "reason": self._status_reasons.get(language, ""),
                    }
                )
        return out

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

        # All servers settled; spin up the watchdog only if at least one is
        # RUNNING (otherwise there's nothing for it to poll).
        with self._lock:
            has_running = any(s == ServerStatus.RUNNING for s in self._status.values())
        if has_running:
            self._ensure_watchdog()

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

    def request_rename(
        self,
        abs_path: str,
        line0: int,
        col0: int,
        new_name: str,
    ) -> dict[str, Any] | None:
        """Run ``textDocument/rename`` and return the raw ``WorkspaceEdit``.

        multilspy 0.0.15 does **not** wrap rename on ``SyncLanguageServer`` (it
        only surfaces definition / references / completions / document_symbols /
        hover / workspace_symbol). The bare LSP request is reachable through the
        inner async server (``language_server.server.send.rename(params)``), so
        this method bridges async→sync using the exact pattern multilspy uses
        internally: schedule the coroutine on the server's own event loop via
        :func:`asyncio.run_coroutine_threadsafe` and block on the result.

        The document is opened (``textDocument/didOpen``) for the duration of the
        request via multilspy's ``open_file`` context manager — the same thing
        ``request_definition`` does internally so the server has the buffer
        loaded before it computes the edit.

        Returns the raw ``WorkspaceEdit`` dict, or ``None`` when no server routes
        the file, the server is unhealthy, or the request yields no edit. The
        multilspy internals are reached through ``getattr`` guards so a future
        version shift degrades to ``None`` instead of an ``AttributeError``.
        """
        sync_server = self.get_server_for_file(abs_path)
        if sync_server is None:
            return None

        relpath = to_repo_relative(abs_path, self._repository_root)
        uri = path_to_document_uri(abs_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": col0},
            "newName": new_name,
        }

        language_server = getattr(sync_server, "language_server", None)
        loop = getattr(sync_server, "loop", None)
        open_file = getattr(language_server, "open_file", None)
        inner = getattr(language_server, "server", None)
        send = getattr(inner, "send", None)
        rename = getattr(send, "rename", None)
        if loop is None or not callable(open_file) or not callable(rename):
            return None

        async def _rename_coro() -> Any:
            # ``open_file`` is a context manager and ``rename`` an async callable
            # on multilspy's untyped internals; cast through Any so the ``with``
            # / ``await`` below type-check (same convention used elsewhere in
            # this module for multilspy-internal access). The callable() guards
            # above narrow these back to ``object``, hence the explicit casts.
            open_cm = cast(Any, open_file)
            rename_fn = cast(Any, rename)
            with open_cm(relpath):
                return await rename_fn(params)

        # multilspy spins its own event-loop thread inside ``start_server``;
        # ``sync_server.loop`` is that loop. Scheduling onto it from this
        # (caller) thread is the only safe way to drive the async server.
        future = asyncio.run_coroutine_threadsafe(_rename_coro(), loop)
        timeout = self._config.start_timeout or 15.0
        result = future.result(timeout=timeout)
        if not isinstance(result, dict):
            return None
        return result

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
        # The watchdog may have been stopped by a previous ``close_all``;
        # reload should bring it back so the recovered server is also
        # monitored.
        self._ensure_watchdog()

    def close_all(self) -> None:
        """Stop every managed server. Idempotent; safe to call on exit.

        Called by ``atexit.register`` *and* the explicit ``finally`` block
        in ``src/main.py``. The watchdog poller is stopped first so it does
        not fire spurious "disconnected" notices while the subprocesses are
        being torn down on purpose.
        """
        # Stop the watchdog before tearing servers down. Otherwise it could
        # observe ``returncode`` flipping non-None during graceful shutdown
        # and mis-report a disconnect.
        self._watchdog_stop.set()
        watchdog = self._watchdog_thread
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=2.0)
        self._watchdog_thread = None

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
        # Install our publishDiagnostics handler now that multilspy has wired
        # ``do_nothing``. Order matters: the bundled adapter registers its
        # handler *inside* ``start_server`` before ``__enter__`` returns, so
        # the moment we land here the slot is filled by ``do_nothing`` and
        # ours replaces it cleanly.
        self._install_diagnostics_handler(server.language, entered, entry)
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

    def _install_diagnostics_handler(
        self,
        language: str,
        sync_server: Any,
        entry: _ServerEntry,
    ) -> None:
        """Reach into multilspy and replace ``do_nothing`` for publishDiagnostics.

        multilspy 0.0.15 registers ``do_nothing`` on every server adapter
        (`multilspy/language_servers/<lang>/<lang>.py` — grep for
        ``textDocument/publishDiagnostics``). The handler dict
        (``on_notification_handlers``) lives at
        ``language_server.server.on_notification_handlers`` on the inner
        ``LanguageServerHandler``. We overwrite the single registered slot
        so future notifications populate ``entry.diagnostics``.

        Best-effort: if the multilspy internals shift in a future version we
        log and continue — diagnostics simply won't update, but everything
        else (outline / definition / references) keeps working.
        """
        try:
            inner = getattr(sync_server, "language_server", None)
            handler = getattr(inner, "server", None) if inner is not None else None
            on_notification = getattr(handler, "on_notification", None)
            if not callable(on_notification):
                _log.debug(
                    "LSP %r: multilspy on_notification missing; "
                    "diagnostics cache disabled.",
                    language,
                )
                return

            async def _on_publish(params: Any) -> None:
                """Cache push diagnostics keyed by the multilspy-relative path."""
                if not isinstance(params, dict):
                    return
                uri = params.get("uri")
                diagnostics = params.get("diagnostics")
                if not isinstance(uri, str) or not isinstance(diagnostics, list):
                    return
                rel = self._uri_to_relpath(uri)
                with entry.diagnostics_lock:
                    entry.diagnostics[rel] = list(diagnostics)
                    # Fire any waiter — Hybrid hook uses this to know an
                    # incremental re-analysis pass landed.
                    event = entry.diagnostics_events.get(rel)
                    if event is None:
                        event = threading.Event()
                        entry.diagnostics_events[rel] = event
                    event.set()

            on_notification("textDocument/publishDiagnostics", _on_publish)
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning(
                "LSP %r: failed to install diagnostics handler: %s",
                language,
                exc,
            )

    def _uri_to_relpath(self, uri: str) -> str:
        """Convert a ``file://`` URI back to a path relative to the repo root.

        Falls back to the raw URI when conversion fails so the cache still
        gets keyed by *something* deterministic per file.
        """
        try:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(uri)
            raw = unquote(parsed.path)
            if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]
            return to_repo_relative(raw, self._repository_root)
        except Exception:
            return uri

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

    # --------------------------------------------------------- on_disconnect

    def _on_disconnect(self, language: str, reason: str) -> None:
        """Mark a language UNHEALTHY and surface the failure to the user.

        Called by the subprocess watchdog when ``process.returncode`` flips
        non-None unexpectedly. Idempotent per server: ``entry.disconnect_seen``
        guards against the poller re-firing for the same exit.

        Sequence (mirrors ``MCPManager._on_disconnect``):

        1. Mark UNHEALTHY under the lock + pop the entry so subsequent
           ``get_server_for_file`` / ``iter_running`` calls skip the dead
           server.
        2. Format a uniform message with the convention
           ``LSP server <lang> disconnected: <reason>``.
        3. Surface through ``console.print_error`` (when present) and post
           through ``notifier.notify`` (when present) so the REPL drains the
           event between LLM turns.
        4. Invoke any user-supplied ``set_on_disconnect`` callback last so
           tests / extensions can observe the event without blocking the
           built-in surfacing path.
        """
        with self._lock:
            entry = self._entries.get(language)
            if entry is not None and entry.disconnect_seen:
                return
            if entry is not None:
                entry.disconnect_seen = True
            current = self._status.get(language)
            if current not in (ServerStatus.STOPPED,):
                self._status[language] = ServerStatus.UNHEALTHY
                self._status_reasons[language] = reason
            # Pop the entry so ``get_server_for_file`` immediately returns
            # ``None`` for callers — same effect as ``MCPManager`` popping
            # the client dict on disconnect.
            self._entries.pop(language, None)

        message = f"LSP server {language!r} disconnected: {reason}"
        if self._console is not None:
            try:
                self._console.print_error(message)
            except Exception:  # pragma: no cover — console must never crash watchdog
                pass
        if self._notifier is not None:
            try:
                self._notifier.notify(f"lsp:{language}", message)
            except Exception:  # pragma: no cover — notification must never crash
                pass
        if self._on_disconnect_callback is not None:
            try:
                self._on_disconnect_callback(language, reason)
            except Exception:  # pragma: no cover — defensive
                pass

    def _ensure_watchdog(self) -> None:
        """Start the subprocess crash poller if not already running.

        multilspy does not expose an ``on_exit`` callback for the launched
        language server. Polling ``process.returncode`` is the only reliable
        signal — it is None while alive and an integer the moment the
        subprocess terminates. The poll interval (0.5s) keeps the watchdog
        responsive without measurable CPU cost.
        """
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        thread = threading.Thread(
            target=self._watchdog_loop,
            name="lsp-watchdog",
            daemon=True,
        )
        self._watchdog_thread = thread
        thread.start()

    def _watchdog_loop(self) -> None:
        """Poll every entry's underlying subprocess for unexpected exit.

        Reads ``language_server.server.process.returncode`` from the inner
        ``LanguageServerHandler``. A non-None returncode while the entry
        still believes it is RUNNING signals a crash; we report via
        ``_on_disconnect`` and stop polling that language.
        """
        # 0.5s interval matches MCP transports' reader cadence and keeps the
        # latency-to-notice under one second on pyright crashes.
        while not self._watchdog_stop.wait(0.5):
            with self._lock:
                entries = list(self._entries.items())
                statuses = dict(self._status)
            for language, entry in entries:
                if statuses.get(language) != ServerStatus.RUNNING:
                    continue
                if entry.disconnect_seen:
                    continue
                returncode = _process_returncode(entry.server)
                if returncode is None:
                    continue
                # Subprocess died — synthesise a reason from whatever exit
                # information multilspy left us.
                reason = f"subprocess exited (returncode={returncode})"
                self._on_disconnect(language, reason)


def _process_returncode(sync_server: Any) -> int | None:
    """Read the underlying subprocess returncode from a multilspy server.

    Returns ``None`` whenever the subprocess is still alive or we can't
    reach it (e.g. multilspy internals shifted). Used by the watchdog to
    detect crashes without coupling to multilspy version specifics.
    """
    try:
        inner = getattr(sync_server, "language_server", None)
        handler = getattr(inner, "server", None) if inner is not None else None
        process = getattr(handler, "process", None) if handler is not None else None
        if process is None:
            return None
        return process.returncode
    except Exception:  # pragma: no cover — defensive
        return None


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
