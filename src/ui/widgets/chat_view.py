from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from rich import box
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from textual.await_remove import AwaitRemove
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static

from src.ui.console import MAX_TOOL_OUTPUT_CHARS, _render_payload
from src.ui.theme import ThemeManager, get_theme


_EntryKind = Literal[
    "separator", "user", "tool_call", "tool_result",
    "status", "error", "assistant_markdown",
]


@dataclass(slots=True)
class _TranscriptEntry:
    kind: _EntryKind
    payload: Any = None
    name: str | None = None


class ChatView(VerticalScroll):
    """Scrollable chat transcript for the Textual UI."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._entries: list[_TranscriptEntry] = []
        self._stream_widget: Static | None = None
        self._stream_chunks: list[str] = []
        self._stream_renderable = Text()
        self._turn_count: int = 0

    def append_user(self, text: str) -> None:
        """Append a user message."""
        if self._turn_count > 0:
            self._append_entry(_TranscriptEntry("separator"))
        self._turn_count += 1
        self._append_entry(_TranscriptEntry("user", text))

    def append_assistant_markdown(self, text: str) -> None:
        """Append an assistant response rendered as Markdown."""
        if not text.strip():
            return
        self._append_entry(_TranscriptEntry("assistant_markdown", text))

    def append_tool_call(self, name: str, data: Any) -> None:
        """Append a tool call panel."""
        self._append_entry(_TranscriptEntry("tool_call", data, name=name))

    def append_tool_result(self, name: str, output: Any) -> None:
        """Append a tool result panel."""
        self._append_entry(_TranscriptEntry("tool_result", output, name=name))

    def append_status(self, msg: str) -> None:
        """Append a dim status message."""
        self._append_entry(_TranscriptEntry("status", msg))

    def append_error(self, msg: str) -> None:
        """Append an error message."""
        self._append_entry(_TranscriptEntry("error", msg))

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
            self._append_entry(_TranscriptEntry("assistant_markdown", full_text))

    def end_stream_and_return(self) -> str:
        """End streaming and return the collected text."""
        full_text = "".join(self._stream_chunks)
        self.end_stream(full_text)
        return full_text

    def rerender_transcript(self) -> None:
        """Rebuild mounted transcript widgets in place using the active theme."""
        transcript_widgets = [
            child for child in self.children if child is not self._stream_widget
        ]
        if len(transcript_widgets) != len(self._entries):
            return

        tm = get_theme()
        for widget, entry in zip(transcript_widgets, self._entries):
            if entry.kind == "assistant_markdown":
                if isinstance(widget, Markdown):
                    widget.update(str(entry.payload))
                continue
            if isinstance(widget, Static):
                widget.update(self._build_renderable(entry, tm))

        self.scroll_end(animate=False)

    def _mount_and_scroll(self, widget: Widget) -> None:
        self.mount(widget)
        self.scroll_end(animate=False)

    def _append_entry(self, entry: _TranscriptEntry) -> None:
        self._entries.append(entry)
        self._mount_and_scroll(self._build_widget(entry))

    def _build_widget(self, entry: _TranscriptEntry) -> Widget:
        if entry.kind == "assistant_markdown":
            return Markdown(str(entry.payload))
        return Static(self._build_renderable(entry))

    def _build_renderable(
        self, entry: _TranscriptEntry, tm: ThemeManager | None = None,
    ) -> Rule | Text | Panel:
        if tm is None:
            tm = get_theme()
        if entry.kind == "separator":
            return Rule(style=tm.palette.text_muted)
        if entry.kind == "user":
            return Text(f"> {entry.payload}", style=f"bold {tm.palette.accent}")
        if entry.kind == "tool_call":
            code, lexer = _render_payload(entry.payload)
            return Panel(
                Syntax(code, lexer, word_wrap=True),
                title=(
                    f"[bold {tm.palette.info}]{tm.icons.tool} Tool Call[/] "
                    f"[{tm.palette.text_muted}]{entry.name or 'unknown'}[/]"
                ),
                border_style=tm.palette.border,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        if entry.kind == "tool_result":
            code, lexer = _render_payload(
                entry.payload,
                max_chars=MAX_TOOL_OUTPUT_CHARS,
            )
            return Panel(
                Syntax(code, lexer, word_wrap=True),
                title=(
                    f"[bold {tm.palette.success}]{tm.icons.success} Result[/] "
                    f"[{tm.palette.text_muted}]{entry.name or 'unknown'}[/]"
                ),
                border_style=tm.palette.border,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        if entry.kind == "status":
            return Text(str(entry.payload), style=tm.palette.text_muted)
        if entry.kind == "error":
            return Text(
                f"{tm.icons.error} {entry.payload}",
                style=f"bold {tm.palette.error}",
            )
        raise ValueError(f"Unsupported transcript entry kind: {entry.kind}")

    def remove_children(
        self,
        selector: str | type[Widget] | list[Widget] = "*",
    ) -> AwaitRemove:
        if selector == "*":
            self._entries = []
            self._stream_widget = None
            self._stream_chunks = []
            self._stream_renderable = Text()
            self._turn_count = 0
        return super().remove_children(selector)
