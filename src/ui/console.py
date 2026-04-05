from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

if TYPE_CHECKING:
    from src.ui.stream import StreamPrinter

MAX_TOOL_OUTPUT_CHARS = 2000


class AgentConsole:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def print_assistant(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(Markdown(text))

    def print_tool_call(self, name: str, input_data: Any) -> None:
        code, lexer = _render_payload(input_data)
        self.console.print(
            Panel(
                Syntax(code, lexer, word_wrap=True),
                title=f"[bold cyan]Tool Call[/bold cyan] {name}",
                border_style="cyan",
            )
        )

    def print_tool_result(self, name: str, output: Any) -> None:
        code, lexer = _render_payload(output, max_chars=MAX_TOOL_OUTPUT_CHARS)
        self.console.print(
            Panel(
                Syntax(code, lexer, word_wrap=True),
                title=f"[bold green]Tool Result[/bold green] {name}",
                border_style="green",
            )
        )

    def print_error(self, msg: str) -> None:
        self.console.print(msg, style="bold red")

    def print_status(self, msg: str) -> None:
        self.console.print(msg, style="dim")

    def get_stream_printer(self) -> StreamPrinter:
        from src.ui.stream import StreamPrinter

        return StreamPrinter(self.console)


def _render_payload(payload: Any, *, max_chars: int | None = None) -> tuple[str, str]:
    if isinstance(payload, (dict, list, tuple)):
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return _truncate(text, max_chars), "json"

    if payload is None:
        return "", "text"

    text = str(payload)
    if _looks_like_json(text):
        return _truncate(text, max_chars), "json"
    return _truncate(text, max_chars), "text"


def _truncate(text: str, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n... [truncated]"


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return True
