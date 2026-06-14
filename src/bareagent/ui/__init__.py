"""UI modules for BareAgent."""

from bareagent.ui.console import AgentConsole
from bareagent.ui.protocol import StreamProtocol, UIProtocol
from bareagent.ui.stream import StreamPrinter
from bareagent.ui.theme import (
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
