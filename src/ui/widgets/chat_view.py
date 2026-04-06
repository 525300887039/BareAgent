from __future__ import annotations

from typing import Any

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static

from src.ui.console import MAX_TOOL_OUTPUT_CHARS, _render_payload


class ChatView(VerticalScroll):
    """Scrollable chat transcript for the Textual UI."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stream_widget: Static | None = None
        self._stream_chunks: list[str] = []
        self._stream_renderable = Text()

    def append_user(self, text: str) -> None:
        """Append a user message."""
        self._mount_and_scroll(Static(Text(f"> {text}", style="bold blue")))

    def append_assistant_markdown(self, text: str) -> None:
        """Append an assistant response rendered as Markdown."""
        if not text.strip():
            return
        self._mount_and_scroll(Markdown(text))

    def append_tool_call(self, name: str, data: Any) -> None:
        """Append a tool call panel."""
        code, lexer = _render_payload(data)
        panel = Panel(
            Syntax(code, lexer, word_wrap=True),
            title=f"[bold cyan]Tool Call[/bold cyan] {name}",
            border_style="cyan",
        )
        self._mount_and_scroll(Static(panel))

    def append_tool_result(self, name: str, output: Any) -> None:
        """Append a tool result panel."""
        code, lexer = _render_payload(output, max_chars=MAX_TOOL_OUTPUT_CHARS)
        panel = Panel(
            Syntax(code, lexer, word_wrap=True),
            title=f"[bold green]Tool Result[/bold green] {name}",
            border_style="green",
        )
        self._mount_and_scroll(Static(panel))

    def append_status(self, msg: str) -> None:
        """Append a dim status message."""
        self._mount_and_scroll(Static(Text(msg, style="dim")))

    def append_error(self, msg: str) -> None:
        """Append an error message."""
        self._mount_and_scroll(Static(Text(msg, style="bold red")))

    def begin_stream(self) -> None:
        """Begin streaming assistant output."""
        if self._stream_widget is not None:
            self._stream_widget.remove()
        self._stream_chunks = []
        self._stream_renderable = Text()
        self._stream_widget = Static(self._stream_renderable, markup=False)
        self._mount_and_scroll(self._stream_widget)

    def feed_stream(self, token: str) -> None:
        """Append a streaming token to the active stream widget."""
        if not token or self._stream_widget is None:
            return
        self._stream_chunks.append(token)
        self._stream_renderable.append(token)
        self._stream_widget.update(self._stream_renderable)
        self.scroll_end(animate=False)

    def end_stream(self, full_text: str) -> None:
        """Replace the stream widget with a rendered Markdown block."""
        if self._stream_widget is not None:
            self._stream_widget.remove()
            self._stream_widget = None
        self._stream_chunks = []
        self._stream_renderable = Text()
        if full_text.strip():
            self._mount_and_scroll(Markdown(full_text))

    def end_stream_and_return(self) -> str:
        """End streaming and return the collected text."""
        full_text = "".join(self._stream_chunks)
        self.end_stream(full_text)
        return full_text

    def _mount_and_scroll(self, widget: Widget) -> None:
        self.mount(widget)
        self.scroll_end(animate=False)
