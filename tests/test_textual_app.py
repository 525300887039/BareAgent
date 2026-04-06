from __future__ import annotations

import threading
from pathlib import Path

import pytest
from textual.widgets import Markdown

from src.main import Config, PermissionConfig, ProviderConfig, SubagentConfig, UIConfig
from src.provider.base import BaseLLMProvider, ThinkingConfig
from src.ui.app import BareAgentApp, TextualStreamPrinter
from src.ui.widgets import ChatView


class ReplayProvider(BaseLLMProvider):
    def create(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise AssertionError("Provider should not be called in Textual widget tests.")

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def _make_config(tmp_path: Path) -> Config:
    return Config(
        provider=ProviderConfig(
            name="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        ),
        permission=PermissionConfig(mode="default", allow=[], deny=[]),
        ui=UIConfig(stream=False, theme="dark"),
        subagent=SubagentConfig(max_depth=3, default_type="general-purpose"),
        thinking=ThinkingConfig(),
        path=tmp_path / "config.toml",
    )


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BareAgentApp:
    monkeypatch.chdir(tmp_path)
    return BareAgentApp(config=_make_config(tmp_path), provider=ReplayProvider())


@pytest.mark.anyio
async def test_shift_tab_binding_triggers_mode_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)
        baseline = len(chat.children)

        await pilot.press("shift+tab")
        await pilot.pause()

        assert len(chat.children) == baseline + 1
        assert app._permission.mode.value == "auto"
        assert "Permission mode: default → auto" in str(chat.children[-1].content)


@pytest.mark.anyio
async def test_textual_stream_printer_restarts_after_tool_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    results: list[str] = []

    async with app.run_test() as pilot:
        printer = TextualStreamPrinter(app)

        def _worker() -> None:
            printer.start()
            printer.feed("alpha")
            results.append(printer.finish())
            printer.feed("beta")
            results.append(printer.finish())

        thread = threading.Thread(target=_worker)
        thread.start()
        while thread.is_alive():
            await pilot.pause()
        thread.join()
        await pilot.pause()

        chat = app.query_one("#chat", ChatView)
        markdowns = [child for child in chat.children if isinstance(child, Markdown)]

        assert results == ["alpha", "beta"]
        assert [markdown.source for markdown in markdowns] == ["alpha", "beta"]


@pytest.mark.anyio
async def test_chat_view_feed_stream_does_not_rejoin_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)

    class AppendOnlyChunks:
        def __init__(self) -> None:
            self.values: list[str] = []

        def append(self, value: str) -> None:
            self.values.append(value)

        def __iter__(self):
            raise AssertionError("feed_stream should not iterate the buffered chunks")

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)
        chat.begin_stream()
        chunks = AppendOnlyChunks()
        chat._stream_chunks = chunks  # type: ignore[assignment]

        chat.feed_stream("token")
        await pilot.pause()

        assert chunks.values == ["token"]
        assert chat._stream_widget is not None
