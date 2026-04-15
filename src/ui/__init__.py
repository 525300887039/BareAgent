"""UI modules for BareAgent."""

from src.ui.console import AgentConsole
from src.ui.protocol import StreamProtocol, UIProtocol
from src.ui.stream import StreamPrinter
from src.ui.theme import (
    ThemeManager,
    format_theme_list,
    format_unknown_theme,
    get_theme,
    init_theme,
)

__all__ = [
    "AgentConsole",
    "StreamPrinter",
    "UIProtocol",
    "StreamProtocol",
    "ThemeManager",
    "get_theme",
    "init_theme",
    "format_theme_list",
    "format_unknown_theme",
]
