from __future__ import annotations

from typing import Any

from rich import box
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static

from src.ui.console import MAX_TOOL_OUTPUT_CHARS, _render_payload
from src.ui.theme import get_theme


class ChatView(VerticalScroll):
    """Scrollable chat transcript for the Textual UI."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stream_widget: Static | None = None
        self._stream_chunks: list[str] = []
        self._stream_renderable = Text()
        self._turn_count: int = 0

    def append_user(self, text: str) -> None:
        """Append a user message."""
        tm = get_theme()
        if self._turn_count > 0:
            self._mount_and_scroll(Static(Rule(style=tm.palette.text_muted)))
        self._turn_count += 1
        self._mount_and_scroll(
            Static(Text(f"> {text}", style=f"bold {tm.palette.accent}"))
        )

    def append_assistant_markdown(self, text: str) -> None:
        """Append an assistant response rendered as Markdown."""
        if not text.strip():
            return
        self._mount_and_scroll(Markdown(text))

    def append_tool_call(self, name: str, data: Any) -> None:
        """Append a tool call panel."""
        tm = get_theme()
        code, lexer = _render_payload(data)
        panel = Panel(
            Syntax(code, lexer, word_wrap=True),
            title=(
                f"[bold {tm.palette.info}]{tm.icons.tool} Tool Call[/] "
                f"[{tm.palette.text_muted}]{name}[/]"
            ),
            border_style=tm.palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )
        self._mount_and_scroll(Static(panel))

    def append_tool_result(self, name: str, output: Any) -> None:
        """Append a tool result panel."""
        tm = get_theme()
        code, lexer = _render_payload(output, max_chars=MAX_TOOL_OUTPUT_CHARS)
        panel = Panel(
            Syntax(code, lexer, word_wrap=True),
            title=(
                f"[bold {tm.palette.success}]{tm.icons.success} Result[/] "
                f"[{tm.palette.text_muted}]{name}[/]"
            ),
            border_style=tm.palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )
        self._mount_and_scroll(Static(panel))

    def append_status(self, msg: str) -> None:
        """Append a dim status message."""
        tm = get_theme()
        self._mount_and_scroll(Static(Text(msg, style=tm.palette.text_muted)))

    def append_error(self, msg: str) -> None:
        """Append an error message."""
        tm = get_theme()
        self._mount_and_scroll(
            Static(Text(f"{tm.icons.error} {msg}", style=f"bold {tm.palette.error}"))
        )

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
