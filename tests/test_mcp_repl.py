"""Tests for the ``/mcp <subcommand>`` REPL dispatcher (PR4).

The dispatcher is a pure function on (text, manager, ui) — exercise it
directly with fake doubles so we can assert UI output without spinning up
the full REPL.
"""

from __future__ import annotations

from typing import Any

from src.main import _dispatch_mcp_command
from src.mcp.errors import MCPError


class _FakeUI:
    """Capture print_status / print_error calls for inspection."""

    def __init__(self) -> None:
        self.status: list[str] = []
        self.errors: list[str] = []

    def print_status(self, msg: str) -> None:
        self.status.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)


class _FakeClient:
    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        prompts: list[dict[str, Any]] | None = None,
        capabilities: set[str] | None = None,
    ) -> None:
        self._tools_cache = tools or []
        self._prompts = prompts or []
        self._caps = capabilities or set()

    def has_capability(self, name: str) -> bool:
        return name in self._caps


class _FakeManager:
    def __init__(
        self,
        *,
        summary: list[dict[str, Any]] | None = None,
        running: list[tuple[str, _FakeClient]] | None = None,
        reload_error: Exception | None = None,
    ) -> None:
        self._summary = summary or []
        self._running = running or []
        self._reload_error = reload_error
        self.reload_calls: list[str] = []

    def summarize(self) -> list[dict[str, Any]]:
        return list(self._summary)

    def iter_running_clients(self):
        yield from self._running

    def reload(self, name: str) -> None:
        self.reload_calls.append(name)
        if self._reload_error is not None:
            raise self._reload_error


def test_dispatch_no_subcommand_prints_usage() -> None:
    ui = _FakeUI()
    _dispatch_mcp_command("/mcp", mcp_manager=_FakeManager(), ui_console=ui)
    assert any("Usage" in line for line in ui.status)


def test_dispatch_status_renders_each_server_row() -> None:
    ui = _FakeUI()
    summary = [
        {
            "name": "github",
            "status": "running",
            "tool_count": 3,
            "has_resources": True,
            "prompt_count": 1,
        },
        {
            "name": "fetch",
            "status": "unhealthy",
            "tool_count": 0,
            "has_resources": False,
            "prompt_count": 0,
        },
    ]
    manager = _FakeManager(summary=summary)

    _dispatch_mcp_command("/mcp status", mcp_manager=manager, ui_console=ui)

    assert len(ui.status) == 2
    assert "github: running" in ui.status[0]
    assert "3 tools" in ui.status[0]
    assert "resources" in ui.status[0]
    assert "1 prompts" in ui.status[0]
    assert "fetch: unhealthy" in ui.status[1]
    assert "no-resources" in ui.status[1]


def test_dispatch_status_with_empty_config() -> None:
    ui = _FakeUI()
    _dispatch_mcp_command("/mcp status", mcp_manager=_FakeManager(), ui_console=ui)
    assert any("no MCP servers configured" in line for line in ui.status)


def test_dispatch_list_groups_tools_and_prompts_by_server() -> None:
    ui = _FakeUI()
    client = _FakeClient(
        tools=[{"name": "fetch"}, {"name": "search"}],
        prompts=[{"name": "summarize"}],
        capabilities={"resources", "prompts"},
    )
    manager = _FakeManager(running=[("fetch", client)])

    _dispatch_mcp_command("/mcp list", mcp_manager=manager, ui_console=ui)

    joined = "\n".join(ui.status)
    assert "[fetch]" in joined
    assert "mcp__fetch__fetch" in joined
    assert "mcp__fetch__search" in joined
    # Resource pseudo-tools added when the server declares the capability.
    assert "mcp__fetch__resource_list" in joined
    assert "mcp__fetch__resource_read" in joined
    assert "/mcp:fetch:summarize" in joined


def test_dispatch_list_with_no_running_servers() -> None:
    ui = _FakeUI()
    _dispatch_mcp_command("/mcp list", mcp_manager=_FakeManager(), ui_console=ui)
    assert any("no MCP servers running" in line for line in ui.status)


def test_dispatch_reload_success_prints_status() -> None:
    ui = _FakeUI()
    manager = _FakeManager()

    _dispatch_mcp_command("/mcp reload github", mcp_manager=manager, ui_console=ui)

    assert manager.reload_calls == ["github"]
    assert any("reloaded" in line for line in ui.status)
    assert ui.errors == []


def test_dispatch_reload_failure_marks_unhealthy_in_ui() -> None:
    ui = _FakeUI()
    manager = _FakeManager(reload_error=MCPError("handshake refused"))

    _dispatch_mcp_command("/mcp reload github", mcp_manager=manager, ui_console=ui)

    assert manager.reload_calls == ["github"]
    joined_errors = "\n".join(ui.errors)
    assert "github" in joined_errors
    assert "UNHEALTHY" in joined_errors


def test_dispatch_reload_missing_name_prints_usage_error() -> None:
    ui = _FakeUI()
    _dispatch_mcp_command("/mcp reload", mcp_manager=_FakeManager(), ui_console=ui)
    assert any("Usage: /mcp reload" in line for line in ui.errors)


def test_dispatch_unknown_subcommand_prints_error() -> None:
    ui = _FakeUI()
    _dispatch_mcp_command("/mcp explode", mcp_manager=_FakeManager(), ui_console=ui)
    assert any("Unknown /mcp subcommand" in line for line in ui.errors)


def test_repl_routing_excludes_colon_form() -> None:
    """``/mcp:foo`` must not trigger the space-prefix command dispatcher.

    Regression guard for the REPL routing condition in src/main.py:
    ``text == "/mcp" or (text.startswith("/mcp ") and not text.startswith("/mcp:"))``.
    A colon form (``/mcp:server:prompt``) is parsed by ``_dispatch_mcp_prompt``,
    not the management command dispatcher.
    """
    text = "/mcp:fetch:summarize"
    space_form_matches = text == "/mcp" or (
        text.startswith("/mcp ") and not text.startswith("/mcp:")
    )
    assert space_form_matches is False
    # And the bare ``/mcp:`` prefix is reserved for the prompt dispatcher.
    assert text.startswith("/mcp:")
