from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from src.ui.theme import ThemeManager, get_theme

if TYPE_CHECKING:
    from src.ui.stream import StreamPrinter

MAX_TOOL_OUTPUT_CHARS = 2000
MAX_PERMISSION_PREVIEW_CHARS = 500


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
        self._permission_choice: str | None = None
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

    def ask_permission(self, name: str, input_data: Any) -> bool:
        theme = get_theme()
        self._permission_choice = None
        self.console.print(
            Panel(
                Syntax(
                    _render_permission_payload(input_data),
                    "json",
                    word_wrap=True,
                ),
                title=(
                    f"[warning]{theme.icons.warning} Permission Required: {name}[/]"
                ),
                subtitle=Text("[y] Allow  [n] Deny  [a] Always allow"),
                border_style=theme.palette.warning,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

        while True:
            try:
                choice = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                self._permission_choice = "deny"
                return False

            if choice == "y":
                self._permission_choice = "allow"
                return True
            if choice == "n":
                self._permission_choice = "deny"
                return False
            if choice == "a":
                self._permission_choice = "always"
                return True

            self.console.print("Press y/n/a", style="permission.ask")

    def consume_permission_choice(self) -> str | None:
        choice = self._permission_choice
        self._permission_choice = None
        return choice

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
    parsed = _try_parse_json(text)
    if parsed is not None:
        # Re-format for consistent indentation.
        formatted = json.dumps(parsed, ensure_ascii=False, indent=2, default=str)
        return _truncate(formatted, max_chars), "json"
    return _truncate(text, max_chars), "text"


def _render_permission_payload(payload: Any) -> str:
    text, _ = _render_payload(payload, max_chars=MAX_PERMISSION_PREVIEW_CHARS)
    return text


def _truncate(text: str, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n... [truncated]"


def _try_parse_json(text: str) -> Any:
    """Return parsed JSON object if *text* looks like JSON, else ``None``."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
