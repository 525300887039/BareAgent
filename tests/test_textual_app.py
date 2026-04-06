from __future__ import annotations

import threading

import pytest
from textual.widgets import Markdown

from src.ui.app import BareAgentApp, TextualStreamPrinter
from src.ui.widgets import ChatView


@pytest.mark.anyio
async def test_shift_tab_binding_triggers_mode_action() -> None:
    app = BareAgentApp(config=None, provider=None)

    async with app.run_test() as pilot:
        chat = app.query_one("#chat", ChatView)

        await pilot.press("shift+tab")
        await pilot.pause()

        assert len(chat.children) == 1
        assert "Permission mode cycling" in str(chat.children[0].content)


@pytest.mark.anyio
async def test_textual_stream_printer_restarts_after_tool_boundary() -> None:
    app = BareAgentApp(config=None, provider=None)
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
async def test_chat_view_feed_stream_does_not_rejoin_buffer() -> None:
    app = BareAgentApp(config=None, provider=None)

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
