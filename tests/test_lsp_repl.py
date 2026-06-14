"""Tests for the ``/lsp`` REPL command and the ``atexit`` registration path.

Exercises the dispatcher in ``src.main._dispatch_lsp_command`` without
spinning up a real ``LanguageServerManager`` — a stub provides the
``summarize`` / ``iter_running`` / ``reload`` surface the dispatcher uses.
"""

from __future__ import annotations

from typing import Any

from bareagent.lsp.errors import LSPError
from bareagent.main import _dispatch_lsp_command, _install_lsp_cleanup


class _StubConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)


class _StubManager:
    def __init__(
        self,
        *,
        summary: list[dict[str, Any]] | None = None,
        running: list[tuple[str, Any]] | None = None,
        reload_raises: BaseException | None = None,
    ) -> None:
        self._summary = summary or []
        self._running = running or []
        self._reload_raises = reload_raises
        self.reload_called_with: str | None = None
        self.close_calls = 0

    def summarize(self) -> list[dict[str, Any]]:
        return list(self._summary)

    def iter_running(self):
        yield from self._running

    def reload(self, language: str) -> None:
        self.reload_called_with = language
        if self._reload_raises is not None:
            raise self._reload_raises

    def close_all(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_status_lists_servers_with_extensions() -> None:
    mgr = _StubManager(
        summary=[
            {
                "language": "python",
                "status": "running",
                "tool_count": 4,
                "extensions": [".py", ".pyi"],
                "reason": "",
            },
            {
                "language": "typescript",
                "status": "unhealthy",
                "tool_count": 0,
                "extensions": [".ts"],
                "reason": "handshake timed out",
            },
        ]
    )
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp status",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    body = "\n".join(console.statuses)
    assert "python: running" in body
    assert "ext=.py, .pyi" in body
    assert "typescript: unhealthy" in body
    assert "handshake timed out" in body


def test_status_when_no_servers_configured() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp status",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("no LSP servers configured" in s for s in console.statuses)


def test_list_lists_four_tools_per_running_server() -> None:
    mgr = _StubManager(running=[("python", object()), ("typescript", object())])
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp list",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    flat = "\n".join(console.statuses)
    for tool in ("lsp_outline", "lsp_definition", "lsp_references", "lsp_diagnostics"):
        # Each tool printed twice (once per server).
        assert flat.count(tool) == 2


def test_list_when_no_running_server() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp list",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("no LSP servers running" in s for s in console.statuses)


def test_reload_invokes_manager() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp reload python",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert mgr.reload_called_with == "python"
    assert any("reloaded" in s for s in console.statuses)


def test_reload_surfaces_failure_as_error() -> None:
    mgr = _StubManager(reload_raises=LSPError("handshake timed out"))
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp reload python",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("reload 'python' failed" in e for e in console.errors)
    assert any("UNHEALTHY" in e for e in console.errors)


def test_reload_requires_argument() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp reload",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("Usage: /lsp reload" in e for e in console.errors)


def test_unknown_subcommand_emits_error() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp blast",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("Unknown /lsp subcommand" in e for e in console.errors)


def test_bare_command_prints_usage() -> None:
    mgr = _StubManager()
    console = _StubConsole()
    _dispatch_lsp_command(
        "/lsp",
        lsp_manager=mgr,  # type: ignore[arg-type]
        ui_console=console,  # type: ignore[arg-type]
    )
    assert any("Usage: /lsp" in s for s in console.statuses)


# ---------------------------------------------------------------------------
# atexit registration
# ---------------------------------------------------------------------------


def test_install_lsp_cleanup_registers_atexit(monkeypatch) -> None:
    registered: list[Any] = []

    def _fake_register(fn, *args, **kwargs):
        registered.append(fn)
        return fn

    monkeypatch.setattr("bareagent.main.atexit.register", _fake_register)

    mgr = _StubManager()
    _install_lsp_cleanup(mgr)  # type: ignore[arg-type]
    assert registered == [mgr.close_all]
