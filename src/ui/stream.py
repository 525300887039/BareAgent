from __future__ import annotations

import sys
from typing import TextIO

from rich.console import Console


class StreamPrinter:
    def __init__(
        self,
        console: Console | None = None,
        *,
        status_message: str = "Thinking...",
        writer: TextIO | None = None,
    ) -> None:
        self.console = console or Console()
        self.status_message = status_message
        console_writer = getattr(self.console, "file", None)
        self.writer = writer or console_writer or sys.stdout
        self._chunks: list[str] = []
        self._active = False
        self._printed_anything = False

    def start(self) -> None:
        if self._active:
            return
        if not self._chunks:
            self.console.print(self.status_message, style="dim")
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
        result = "".join(self._chunks)
        self._chunks = []
        self._printed_anything = False
        return result
