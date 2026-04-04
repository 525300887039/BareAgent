"""Tests for runtime permission mode switching commands."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.main import (
    _MODE_CYCLE,
    _PERMISSION_SLASH,
    _handle_mode_interactive,
    _handle_shift_tab_mode_cycle,
    _next_permission_mode,
)
from src.permission.guard import PermissionGuard, PermissionMode


# ---------------------------------------------------------------------------
# Direct slash commands: /default, /auto, /plan, /bypass
# ---------------------------------------------------------------------------


class TestPermissionSlashMapping:
    def test_all_modes_mapped(self):
        assert set(_PERMISSION_SLASH.values()) == set(PermissionMode)

    @pytest.mark.parametrize(
        "cmd, expected",
        [
            ("/default", PermissionMode.DEFAULT),
            ("/auto", PermissionMode.AUTO),
            ("/plan", PermissionMode.PLAN),
            ("/bypass", PermissionMode.BYPASS),
        ],
    )
    def test_slash_maps_to_correct_mode(self, cmd, expected):
        assert _PERMISSION_SLASH[cmd] is expected


class TestDirectModeSwitch:
    """Simulate the REPL branch: ``if text in _PERMISSION_SLASH``."""

    def _switch(self, guard: PermissionGuard, cmd: str) -> PermissionMode:
        old = guard.mode
        guard.mode = _PERMISSION_SLASH[cmd]
        return old

    @pytest.mark.parametrize("target", ["/auto", "/bypass", "/plan", "/default"])
    def test_switch_changes_mode(self, target):
        guard = PermissionGuard(PermissionMode.DEFAULT)
        self._switch(guard, target)
        assert guard.mode is _PERMISSION_SLASH[target]

    def test_switch_returns_old_mode(self):
        guard = PermissionGuard(PermissionMode.AUTO)
        old = self._switch(guard, "/plan")
        assert old is PermissionMode.AUTO
        assert guard.mode is PermissionMode.PLAN


# ---------------------------------------------------------------------------
# /mode interactive selection
# ---------------------------------------------------------------------------


class TestHandleModeInteractive:
    def _make_guard(self, mode=PermissionMode.DEFAULT):
        return PermissionGuard(mode)

    def _make_console(self):
        console = MagicMock()
        console.print_status = MagicMock()
        return console

    @pytest.mark.parametrize("choice, expected", [
        ("1", PermissionMode.DEFAULT),
        ("2", PermissionMode.AUTO),
        ("3", PermissionMode.PLAN),
        ("4", PermissionMode.BYPASS),
    ])
    def test_valid_choice(self, choice, expected):
        guard = self._make_guard(PermissionMode.DEFAULT)
        ui = self._make_console()
        with patch("builtins.input", return_value=choice):
            _handle_mode_interactive(guard, ui)
        assert guard.mode is expected

    def test_invalid_choice_keeps_mode(self):
        guard = self._make_guard(PermissionMode.AUTO)
        ui = self._make_console()
        with patch("builtins.input", return_value="x"):
            _handle_mode_interactive(guard, ui)
        assert guard.mode is PermissionMode.AUTO
        ui.print_status.assert_any_call("Invalid choice, mode unchanged.")

    def test_eof_cancels(self):
        guard = self._make_guard(PermissionMode.DEFAULT)
        ui = self._make_console()
        with patch("builtins.input", side_effect=EOFError):
            _handle_mode_interactive(guard, ui)
        assert guard.mode is PermissionMode.DEFAULT
        ui.print_status.assert_any_call("Mode selection cancelled.")

    def test_keyboard_interrupt_cancels(self):
        guard = self._make_guard(PermissionMode.DEFAULT)
        ui = self._make_console()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            _handle_mode_interactive(guard, ui)
        assert guard.mode is PermissionMode.DEFAULT

    def test_menu_shows_current_marker(self):
        guard = self._make_guard(PermissionMode.PLAN)
        ui = self._make_console()
        with patch("builtins.input", return_value="3"):
            _handle_mode_interactive(guard, ui)
        menu_text = ui.print_status.call_args_list[0][0][0]
        assert "* 3) plan" in menu_text
        assert "  1) default" in menu_text


# ---------------------------------------------------------------------------
# _MODE_CYCLE correctness
# ---------------------------------------------------------------------------


class TestModeCycle:
    def test_cycle_contains_all_modes(self):
        assert set(_MODE_CYCLE) == set(PermissionMode)

    def test_cycle_order(self):
        assert _MODE_CYCLE == [
            PermissionMode.DEFAULT,
            PermissionMode.AUTO,
            PermissionMode.PLAN,
            PermissionMode.BYPASS,
        ]

    def test_cycle_wraps_around(self):
        """Simulates Shift+Tab cycling logic."""
        mode = PermissionMode.BYPASS
        assert _next_permission_mode(mode) is PermissionMode.DEFAULT

    @pytest.mark.parametrize(
        "current, expected_next",
        [
            (PermissionMode.DEFAULT, PermissionMode.AUTO),
            (PermissionMode.AUTO, PermissionMode.PLAN),
            (PermissionMode.PLAN, PermissionMode.BYPASS),
            (PermissionMode.BYPASS, PermissionMode.DEFAULT),
        ],
    )
    def test_full_cycle(self, current, expected_next):
        assert _next_permission_mode(current) is expected_next


# ---------------------------------------------------------------------------
# Shift+Tab equivalent command
# ---------------------------------------------------------------------------


class TestShiftTabEquivalent:
    def test_cycles_mode_without_overwriting_pending_input(self):
        guard = PermissionGuard(PermissionMode.DEFAULT)
        ui = MagicMock()
        ui.print_status = MagicMock()
        buffer = SimpleNamespace(text="draft prompt", validate_and_handle=MagicMock())
        event = SimpleNamespace(current_buffer=buffer)

        _handle_shift_tab_mode_cycle(event, guard, ui)

        assert guard.mode is PermissionMode.AUTO
        assert buffer.text == "draft prompt"
        buffer.validate_and_handle.assert_not_called()
        ui.print_status.assert_called_with("Permission mode: default → auto")
