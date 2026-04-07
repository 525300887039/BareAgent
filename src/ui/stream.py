from __future__ import annotations

import sys
from typing import TextIO

from rich.console import Console
from src.ui.theme import get_theme


class StreamPrinter:
    def __init__(
        self,
        console: Console | None = None,
        *,
        status_message: str = "Thinking...",
        writer: TextIO | None = None,
    ) -> None:
        tm = get_theme()
        self.console = console or Console(
            no_color=tm.no_color,
        )
        self.status_message = status_message
        console_writer = getattr(self.console, "file", None)
        self.writer = writer or console_writer or sys.stdout
        self._chunks: list[str] = []
        self._active = False
        self._printed_anything = False
        self._theme_pushed = False

    def start(self) -> None:
        if self._active:
            return
        tm = get_theme()
        if not self._theme_pushed:
            self.console.push_theme(tm.rich_theme)
            self._theme_pushed = True
        if not self._chunks:
            self.console.print(
                f"{tm.icons.running} {self.status_message}",
                style="status",
            )
        self._active = True

    def feed(self, token: str) -> None:
        if not token:
            return
        if not self._active:
            self.start()
        self._chunks.append(token)
        self.writer.write(token)
        self.writer.flush()
        self._printed_anything = True

    def finish(self) -> str:
        if self._active and self._printed_anything:
            self.writer.write("\n")
            self.writer.flush()
        self._active = False
        if self._theme_pushed:
            self.console.pop_theme()
            self._theme_pushed = False
        result = "".join(self._chunks)
        self._chunks = []
        self._printed_anything = False
        return result
