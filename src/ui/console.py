from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from src.ui.theme import ThemeManager, get_theme

if TYPE_CHECKING:
    from src.ui.stream import StreamPrinter

MAX_TOOL_OUTPUT_CHARS = 2000


class AgentConsole:
    def __init__(
        self,
        console: Console | None = None,
        theme: ThemeManager | None = None,
    ) -> None:
        tm = theme or get_theme()
        self.console = console or Console(
            no_color=tm.no_color,
        )
        self._theme_pushed = False
        self.set_theme(tm)

    def set_theme(self, theme: ThemeManager | None = None) -> None:
        tm = theme or get_theme()
        if self._theme_pushed:
            self.console.pop_theme()
        self.console.push_theme(tm.rich_theme)
        self._theme_pushed = True

    def print_assistant(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(Markdown(text))

    def print_tool_call(self, name: str, input_data: Any) -> None:
        icons = get_theme().icons
        code, lexer = _render_payload(input_data)
        self.console.print(
            Panel(
                Syntax(code, lexer, word_wrap=True),
                title=f"[tool.name]{icons.tool} Tool Call[/] [muted]{name}[/]",
                border_style="tool.border",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def print_tool_result(self, name: str, output: Any) -> None:
        icons = get_theme().icons
        code, lexer = _render_payload(output, max_chars=MAX_TOOL_OUTPUT_CHARS)
        self.console.print(
            Panel(
                Syntax(code, lexer, word_wrap=True),
                title=f"[success]{icons.success} Result[/] [muted]{name}[/]",
                border_style="result.border",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def print_error(self, msg: str) -> None:
        icons = get_theme().icons
        self.console.print(f"{icons.error} {msg}", style="error")

    def print_status(self, msg: str) -> None:
        self.console.print(msg, style="status")

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
