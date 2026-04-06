from __future__ import annotations

import pytest
from rich.theme import Theme

from src.ui import theme as theme_module
from src.ui.theme import DEFAULT_THEME_NAME, PALETTES, ThemeManager


@pytest.fixture(autouse=True)
def reset_theme_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    theme_module._manager = None
    yield
    theme_module._manager = None


def test_theme_manager_uses_default_theme() -> None:
    manager = ThemeManager()

    assert manager.name == DEFAULT_THEME_NAME
    assert manager.palette == PALETTES[DEFAULT_THEME_NAME]


def test_theme_manager_uses_valid_theme_name() -> None:
    manager = ThemeManager("dracula")

    assert manager.name == "dracula"
    assert manager.palette == PALETTES["dracula"]


def test_theme_manager_falls_back_to_default_for_invalid_theme() -> None:
    manager = ThemeManager("nonexistent")

    assert manager.name == DEFAULT_THEME_NAME
    assert manager.palette == PALETTES[DEFAULT_THEME_NAME]


def test_switch_changes_theme_successfully() -> None:
    manager = ThemeManager()

    assert manager.switch("dracula") is True
    assert manager.name == "dracula"
    assert manager.palette.error == "#ff5555"


def test_switch_rejects_invalid_theme_and_keeps_current_theme() -> None:
    manager = ThemeManager("nord")
    original_palette = manager.palette

    assert manager.switch("nonexistent") is False
    assert manager.name == "nord"
    assert manager.palette == original_palette


def test_available_themes_returns_all_theme_names() -> None:
    assert ThemeManager.available_themes() == [
        "catppuccin-mocha",
        "dracula",
        "nord",
        "tokyo-night",
        "gruvbox",
    ]


def test_theme_manager_detects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    manager = ThemeManager()

    assert manager.no_color is True


def test_rich_theme_property_returns_rich_theme_instance() -> None:
    manager = ThemeManager()

    assert isinstance(manager.rich_theme, Theme)


def test_init_theme_and_get_theme_share_global_singleton() -> None:
    default_manager = theme_module.get_theme()
    same_manager = theme_module.get_theme()

    assert default_manager is same_manager
    assert default_manager.name == DEFAULT_THEME_NAME

    dracula_manager = theme_module.init_theme("dracula")

    assert dracula_manager is theme_module.get_theme()
    assert dracula_manager is not default_manager
    assert dracula_manager.name == "dracula"
