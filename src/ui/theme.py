from __future__ import annotations

import os
from dataclasses import dataclass

from rich.theme import Theme


@dataclass(frozen=True, slots=True)
class ColorPalette:
    """A complete semantic color palette for the UI."""

    bg: str
    bg_deep: str
    surface: str
    surface_active: str
    border: str
    text: str
    text_secondary: str
    text_muted: str
    success: str
    error: str
    warning: str
    info: str
    accent: str
    keyword: str


@dataclass(frozen=True, slots=True)
class Icons:
    """Safe Unicode status icons that do not require Nerd Font."""

    success: str = "✓"
    error: str = "✗"
    warning: str = "⚠"
    info: str = "●"
    pending: str = "○"
    running: str = "▶"
    tool: str = "⚡"


PALETTES: dict[str, ColorPalette] = {
    "catppuccin-mocha": ColorPalette(
        bg="#1e1e2e",
        bg_deep="#11111b",
        surface="#313244",
        surface_active="#45475a",
        border="#585b70",
        text="#cdd6f4",
        text_secondary="#bac2de",
        text_muted="#6c7086",
        success="#a6e3a1",
        error="#f38ba8",
        warning="#fab387",
        info="#89dceb",
        accent="#89b4fa",
        keyword="#cba6f7",
    ),
    "dracula": ColorPalette(
        bg="#282a36",
        bg_deep="#21222c",
        surface="#44475a",
        surface_active="#6272a4",
        border="#6272a4",
        text="#f8f8f2",
        text_secondary="#f8f8f2",
        text_muted="#6272a4",
        success="#50fa7b",
        error="#ff5555",
        warning="#ffb86c",
        info="#8be9fd",
        accent="#bd93f9",
        keyword="#ff79c6",
    ),
    "nord": ColorPalette(
        bg="#2e3440",
        bg_deep="#2e3440",
        surface="#3b4252",
        surface_active="#434c5e",
        border="#4c566a",
        text="#d8dee9",
        text_secondary="#e5e9f0",
        text_muted="#4c566a",
        success="#a3be8c",
        error="#bf616a",
        warning="#d08770",
        info="#88c0d0",
        accent="#81a1c1",
        keyword="#b48ead",
    ),
    "tokyo-night": ColorPalette(
        bg="#24283b",
        bg_deep="#1f2335",
        surface="#292e42",
        surface_active="#3b4261",
        border="#565f89",
        text="#c0caf5",
        text_secondary="#a9b1d6",
        text_muted="#565f89",
        success="#9ece6a",
        error="#f7768e",
        warning="#ff9e64",
        info="#7dcfff",
        accent="#7aa2f7",
        keyword="#bb9af7",
    ),
    "gruvbox": ColorPalette(
        bg="#282828",
        bg_deep="#1d2021",
        surface="#3c3836",
        surface_active="#504945",
        border="#665c54",
        text="#ebdbb2",
        text_secondary="#d5c4a1",
        text_muted="#665c54",
        success="#b8bb26",
        error="#fb4934",
        warning="#fe8019",
        info="#83a598",
        accent="#83a598",
        keyword="#d3869b",
    ),
}

DEFAULT_THEME_NAME = "catppuccin-mocha"


class ThemeManager:
    """Central theme access for Rich styling and semantic tokens."""

    def __init__(self, name: str = DEFAULT_THEME_NAME) -> None:
        self._name = name if name in PALETTES else DEFAULT_THEME_NAME
        self._palette = PALETTES[self._name]
        self._icons = Icons()
        self._no_color = os.environ.get("NO_COLOR") is not None
        self._rich_theme = self._build_rich_theme()

    @property
    def name(self) -> str:
        return self._name

    @property
    def palette(self) -> ColorPalette:
        return self._palette

    @property
    def icons(self) -> Icons:
        return self._icons

    @property
    def no_color(self) -> bool:
        return self._no_color

    @property
    def rich_theme(self) -> Theme:
        return self._rich_theme

    def switch(self, name: str) -> bool:
        """Switch to a new theme and return whether the switch succeeded."""
        if name not in PALETTES:
            return False
        self._name = name
        self._palette = PALETTES[name]
        self._rich_theme = self._build_rich_theme()
        return True

    @staticmethod
    def available_themes() -> list[str]:
        return list(PALETTES.keys())

    def _build_rich_theme(self) -> Theme:
        p = self._palette
        return Theme(
            {
                "success": f"bold {p.success}",
                "error": f"bold {p.error}",
                "warning": f"bold {p.warning}",
                "info": p.info,
                "muted": p.text_muted,
                "accent": f"bold {p.accent}",
                "user.prompt": f"bold {p.accent}",
                "assistant.text": p.text,
                "tool.name": f"bold {p.info}",
                "tool.border": p.border,
                "result.border": p.border,
                "error.border": p.error,
                "status": p.text_muted,
                "permission.ask": f"bold {p.warning}",
                "separator": p.text_muted,
            }
        )


_manager: ThemeManager | None = None


def init_theme(name: str = DEFAULT_THEME_NAME) -> ThemeManager:
    """Initialize the global theme manager."""
    global _manager
    _manager = ThemeManager(name)
    return _manager


def get_theme() -> ThemeManager:
    """Return the global theme manager, creating it on first access."""
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager


__all__ = [
    "ColorPalette",
    "Icons",
    "PALETTES",
    "DEFAULT_THEME_NAME",
    "ThemeManager",
    "init_theme",
    "get_theme",
]
