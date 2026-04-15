from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Markdown

from src.permission.guard import PermissionMode
from src.provider.base import BaseLLMProvider, LLMResponse
from src.ui.theme import ThemeManager, get_theme
from src.ui.app import BareAgentApp
from src.ui.widgets import ChatView, InputBar, PermissionModal

from tests.conftest import make_test_config


class ReplayProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse | str] | None = None) -> None:
        self.responses = list(responses or [])

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        if not self.responses:
            return LLMResponse(
                text="ok",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=1,
                output_tokens=1,
            )
        response = self.responses.pop(0)
        if isinstance(response, str):
            return LLMResponse(
                text=response,
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=1,
                output_tokens=1,
            )
        return response

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _factory() -> BareAgentApp:
        monkeypatch.chdir(tmp_path)
        return BareAgentApp(
            config=make_test_config(tmp_path),
            provider=ReplayProvider(),
        )

    return _factory


def _widget_text(widget: Any) -> str:
    if isinstance(widget, Markdown):
        return widget.source
    return str(getattr(widget, "content", ""))


@pytest.mark.anyio
async def test_chat_view_append_user(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)
        baseline = len(chat.children)

        chat.append_user("Hello")
        await pilot.pause()

        assert len(chat.children) == baseline + 1
        assert "> Hello" in _widget_text(chat.children[-1])


@pytest.mark.anyio
async def test_chat_view_reset_clears_turn_separator_state(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)

        chat.append_user("First")
        chat.append_user("Second")
        await pilot.pause()
        await chat.remove_children()
        await pilot.pause()

        chat.append_user("After reset")
        await pilot.pause()

        assert len(chat.children) == 1
        assert "> After reset" in _widget_text(chat.children[-1])


@pytest.mark.anyio
async def test_chat_view_stream_flow(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)

        chat.begin_stream()
        chat.feed_stream("Hel")
        chat.feed_stream("lo")
        await pilot.pause()
        result = chat.end_stream_and_return()
        await pilot.pause()

        assert result == "Hello"
        assert isinstance(chat.children[-1], Markdown)
        assert chat.children[-1].source == "Hello"


@pytest.mark.anyio
async def test_input_bar_submit(
    make_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app()
    captured: list[str] = []
    monkeypatch.setattr(app, "run_agent_turn", lambda value: captured.append(value))

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        input_bar.value = "test message"
        await pilot.press("enter")
        await pilot.pause()

        assert input_bar.value == ""
        assert captured == ["test message"]


@pytest.mark.anyio
async def test_permission_modal_allow(make_app) -> None:
    app = make_app()
    results: list[bool] = []

    async with app.run_test() as pilot:
        app.push_screen(
            PermissionModal("bash", {"command": "ls"}),
            callback=results.append,
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert results == [True]


@pytest.mark.anyio
async def test_permission_modal_deny(make_app) -> None:
    app = make_app()
    results: list[bool] = []

    async with app.run_test() as pilot:
        app.push_screen(
            PermissionModal("bash", {"command": "rm -rf /"}),
            callback=results.append,
        )
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        assert results == [False]


@pytest.mark.anyio
async def test_slash_help_command(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)

        input_bar.value = "/help"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert "Available commands:" in rendered
        assert "/help      Show this help message" in rendered


@pytest.mark.anyio
async def test_slash_log_command_routes_to_shared_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_test_config(tmp_path)
    config.debug.enabled = True
    monkeypatch.chdir(tmp_path)
    app = BareAgentApp(config=config, provider=ReplayProvider())
    captured: dict[str, Any] = {}

    def _fake_handle_log_command(
        text: str,
        *,
        config: Any,
        interaction_logger: Any,
        viewer_server: Any,
        print_status: Any,
    ) -> str:
        captured["text"] = text
        captured["config"] = config
        captured["interaction_logger"] = interaction_logger
        captured["viewer_server"] = viewer_server
        print_status("debug status")
        return "viewer-token"

    monkeypatch.setattr(
        "src.ui.app.main_module._handle_log_command",
        _fake_handle_log_command,
    )

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)
        input_bar.value = "/log status"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert captured["text"] == "/log status"
        assert captured["config"] is config
        assert captured["interaction_logger"] is app._interaction_logger
        assert captured["viewer_server"] is None
        assert app._viewer_server == "viewer-token"
        assert "debug status" in rendered


@pytest.mark.anyio
async def test_slash_exit_command(
    make_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app()
    exit_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        app,
        "exit",
        lambda *args, **kwargs: exit_calls.append((args, kwargs)),
    )

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        input_bar.value = "/exit"
        await pilot.press("enter")
        await pilot.pause()

        assert exit_calls


@pytest.mark.anyio
async def test_slash_mode_command_consumes_next_numeric_input(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)

        input_bar.value = "/mode"
        await pilot.press("enter")
        await pilot.pause()

        input_bar.value = "2"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert app._permission.mode is PermissionMode.AUTO
        assert "Permission mode: default → auto" in rendered


@pytest.mark.anyio
async def test_slash_new_and_resume_keep_logger_session_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_test_config(tmp_path)
    config.debug.enabled = True
    monkeypatch.chdir(tmp_path)
    app = BareAgentApp(config=config, provider=ReplayProvider())

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        original_session = app._session_id
        app._transcript_mgr.save(app._messages, original_session)

        input_bar.value = "/new"
        await pilot.press("enter")
        await pilot.pause()

        assert app._interaction_logger.session_id == app._session_id
        assert app._session_id != original_session

        input_bar.value = f"/resume {original_session}"
        await pilot.press("enter")
        await pilot.pause()

        assert app._session_id == original_session
        assert app._interaction_logger.session_id == original_session


@pytest.mark.anyio
async def test_on_mount_initializes_theme_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = make_test_config(tmp_path)
    config.ui.theme = "nord"
    app = BareAgentApp(config=config, provider=ReplayProvider())

    async with app.run_test() as pilot:
        await pilot.pause()

        assert get_theme().name == "nord"


@pytest.mark.anyio
async def test_slash_theme_lists_available_themes_and_marks_current(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)

        input_bar.value = "/theme"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert "Available themes:" in rendered
        assert "● catppuccin-mocha" in rendered
        assert "Usage: /theme <name>" in rendered
        for theme_name in ThemeManager.available_themes():
            assert theme_name in rendered


@pytest.mark.anyio
async def test_slash_theme_switches_theme(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)

        input_bar.value = "/theme dracula"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert get_theme().name == "dracula"
        assert "Theme switched to: dracula" in rendered


@pytest.mark.anyio
async def test_slash_theme_rerenders_existing_transcript(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)
        initial_status_style = chat.children[0].content.style

        input_bar.value = "/theme dracula"
        await pilot.press("enter")
        await pilot.pause()

        assert chat.children[0].content.style == get_theme().palette.text_muted
        assert chat.children[0].content.style != initial_status_style
        assert "> /theme dracula" in _widget_text(chat.children[-2])
        assert chat.children[-2].content.style == f"bold {get_theme().palette.accent}"


@pytest.mark.anyio
async def test_slash_theme_rejects_unknown_theme(make_app) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        chat = app.query_one("#chat", ChatView)

        input_bar.value = "/theme nonexistent"
        await pilot.press("enter")
        await pilot.pause()

        rendered = "\n".join(_widget_text(child) for child in chat.children)
        assert get_theme().name == "catppuccin-mocha"
        assert "Unknown theme: nonexistent." in rendered
        assert ", ".join(ThemeManager.available_themes()) in rendered


@pytest.mark.anyio
async def test_shift_tab_cycles_permission_mode_without_touching_input(
    make_app,
) -> None:
    app = make_app()

    async with app.run_test() as pilot:
        input_bar = app.query_one("#input", InputBar)
        input_bar.value = "draft prompt"

        await pilot.press("shift+tab")
        await pilot.pause()

        assert app._permission.mode is PermissionMode.AUTO
        assert input_bar.value == "draft prompt"


@pytest.mark.anyio
async def test_startup_load_errors_are_visible_in_chat(
    make_app,
    tmp_path: Path,
) -> None:
    (tmp_path / ".tasks.json").write_text("{broken-json", encoding="utf-8")
    (tmp_path / ".team.json").write_text("{broken-json", encoding="utf-8")
    app = make_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        rendered = "\n".join(_widget_text(child) for child in chat.children)

        assert "Failed to load task file" in rendered
        assert "Failed to load team file" in rendered
