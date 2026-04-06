"""Tests for exported permission mode constants in ``src.main``."""

from __future__ import annotations

import pytest

from src.main import _MODE_CYCLE, _PERMISSION_SLASH
from src.permission.guard import PermissionMode


def test_permission_slash_maps_all_modes() -> None:
    assert set(_PERMISSION_SLASH.values()) == set(PermissionMode)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/default", PermissionMode.DEFAULT),
        ("/auto", PermissionMode.AUTO),
        ("/plan", PermissionMode.PLAN),
        ("/bypass", PermissionMode.BYPASS),
    ],
)
def test_permission_slash_mapping(command: str, expected: PermissionMode) -> None:
    assert _PERMISSION_SLASH[command] is expected


def test_mode_cycle_contains_all_modes() -> None:
    assert set(_MODE_CYCLE) == set(PermissionMode)


def test_mode_cycle_order() -> None:
    assert _MODE_CYCLE == [
        PermissionMode.DEFAULT,
        PermissionMode.AUTO,
        PermissionMode.PLAN,
        PermissionMode.BYPASS,
    ]
