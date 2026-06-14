"""Tests for ROADMAP 4.3 config hot-reload (`/reload` + passive mtime detect).

Covers the pure diff/classification logic (`_diff_config_for_reload`), the
config-file mtime helper (`_config_mtimes`), and the `_dispatch_reload_command`
failure-safety + hot-apply behavior.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from bareagent.main import (
    PermissionConfig,
    UIConfig,
    _config_mtimes,
    _diff_config_for_reload,
    _dispatch_reload_command,
)
from bareagent.permission.guard import PermissionGuard, PermissionMode
from bareagent.ui.console import AgentConsole
from bareagent.ui.theme import init_theme
from tests.conftest import make_test_config


class _FakeConsole:
    """Records status/error calls; no real rich output."""

    def __init__(self) -> None:
        self.status: list[str] = []
        self.errors: list[str] = []
        self.themes_set = 0

    def print_status(self, msg: str) -> None:
        self.status.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)

    def set_theme(self, theme=None) -> None:  # noqa: ANN001 - test double
        self.themes_set += 1


# --------------------------------------------------------------------------- #
# _diff_config_for_reload
# --------------------------------------------------------------------------- #


def test_diff_theme_change_is_hot(tmp_path: Path) -> None:
    old = make_test_config(tmp_path)
    new = make_test_config(tmp_path)
    new.ui = UIConfig(stream=new.ui.stream, theme="dracula")

    report = _diff_config_for_reload(old, new)

    assert report.changed is True
    assert [c.path for c in report.hot] == ["ui.theme"]
    assert report.hot[0].old == "catppuccin-mocha"
    assert report.hot[0].new == "dracula"
    assert report.restart == []


def test_diff_permission_fields_are_hot(tmp_path: Path) -> None:
    old = make_test_config(tmp_path)
    new = make_test_config(tmp_path)
    new.permission = PermissionConfig(
        mode="auto",
        allow=["bash(prefix:ls*)"],
        deny=["bash(prefix:rm*)"],
    )

    report = _diff_config_for_reload(old, new)

    hot_paths = {c.path for c in report.hot}
    assert hot_paths == {"permission.mode", "permission.allow", "permission.deny"}
    assert report.restart == []


def test_diff_provider_and_other_sections_require_restart(tmp_path: Path) -> None:
    old = make_test_config(tmp_path)
    new = make_test_config(tmp_path)
    new.provider = replace(new.provider, model="gpt-4o")
    new.retry = replace(new.retry, max_attempts=99)
    new.cost.prices = {"gpt-4o": {"input": 1.0, "output": 2.0}}

    report = _diff_config_for_reload(old, new)

    restart_paths = {c.path for c in report.restart}
    assert "provider.model" in restart_paths
    assert "retry.max_attempts" in restart_paths
    assert "cost.prices" in restart_paths
    assert report.hot == []


def test_diff_no_change_returns_empty_report(tmp_path: Path) -> None:
    old = make_test_config(tmp_path)
    new = make_test_config(tmp_path)

    report = _diff_config_for_reload(old, new)

    assert report.changed is False
    assert report.hot == []
    assert report.restart == []


def test_diff_path_field_is_not_a_change(tmp_path: Path) -> None:
    old = make_test_config(tmp_path)
    new = make_test_config(tmp_path)
    new.path = tmp_path / "elsewhere" / "config.toml"

    report = _diff_config_for_reload(old, new)

    assert report.changed is False
    assert all(c.path != "path" for c in report.hot + report.restart)


# --------------------------------------------------------------------------- #
# _config_mtimes
# --------------------------------------------------------------------------- #


def test_config_mtimes_skips_missing_files(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    # No config.toml written to disk in the fixture.
    assert _config_mtimes(config) == {}


def test_config_mtimes_returns_mtime_for_existing_main_and_local(
    tmp_path: Path,
) -> None:
    config = make_test_config(tmp_path)
    main_path = config.path
    local_path = main_path.with_name("config.local.toml")
    main_path.write_text("[ui]\ntheme = 'dracula'\n", encoding="utf-8")
    local_path.write_text("[permission]\nmode = 'auto'\n", encoding="utf-8")

    mtimes = _config_mtimes(config)

    assert str(main_path) in mtimes
    assert str(local_path) in mtimes
    assert mtimes[str(main_path)] == os.stat(main_path).st_mtime


def test_config_mtimes_changes_when_file_touched(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    main_path = config.path
    main_path.write_text("x = 1\n", encoding="utf-8")
    before = _config_mtimes(config)

    # Bump mtime explicitly so the test is not at the mercy of clock resolution.
    new_time = os.stat(main_path).st_mtime + 10
    os.utime(main_path, (new_time, new_time))
    after = _config_mtimes(config)

    assert before != after


# --------------------------------------------------------------------------- #
# _dispatch_reload_command — failure safety
# --------------------------------------------------------------------------- #


def test_reload_failure_keeps_current_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_test_config(tmp_path)
    config.ui.theme = "catppuccin-mocha"
    config.permission.mode = "default"
    permission = PermissionGuard(PermissionMode.DEFAULT)
    console = _FakeConsole()

    def _boom(_path):  # noqa: ANN001, ANN202 - test double
        raise ValueError("bad TOML at line 3")

    monkeypatch.setattr("bareagent.main.load_config", _boom)

    _dispatch_reload_command(
        config=config,
        permission=permission,
        ui_console=cast(AgentConsole, console),
    )

    # Config + guard untouched; error surfaced; no exception escaped.
    assert config.ui.theme == "catppuccin-mocha"
    assert config.permission.mode == "default"
    assert permission.mode == PermissionMode.DEFAULT
    assert console.errors
    assert "Keeping current config" in console.errors[0]


# --------------------------------------------------------------------------- #
# _dispatch_reload_command — hot apply
# --------------------------------------------------------------------------- #


def test_reload_applies_theme_and_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_theme("catppuccin-mocha")  # deterministic starting theme
    config = make_test_config(tmp_path)
    config.ui.theme = "catppuccin-mocha"
    config.permission.mode = "default"
    config.permission.allow = []
    config.permission.deny = []
    permission = PermissionGuard(PermissionMode.DEFAULT)
    console = _FakeConsole()

    new_config = make_test_config(tmp_path)
    new_config.ui.theme = "dracula"
    new_config.permission.mode = "auto"
    new_config.permission.allow = ["bash(prefix:ls*)"]
    new_config.permission.deny = ["bash(prefix:rm*)"]

    monkeypatch.setattr("bareagent.main.load_config", lambda _path: new_config)

    _dispatch_reload_command(
        config=config,
        permission=permission,
        ui_console=cast(AgentConsole, console),
    )

    # Live runtime objects reflect the new values.
    assert permission.mode == PermissionMode.AUTO
    assert permission.allow_rules == ["bash(prefix:ls*)"]
    assert permission.deny_rules == ["bash(prefix:rm*)"]
    # Live config mirrors the applied hot fields.
    assert config.ui.theme == "dracula"
    assert config.permission.mode == "auto"
    assert config.permission.allow == ["bash(prefix:ls*)"]
    assert config.permission.deny == ["bash(prefix:rm*)"]
    assert console.themes_set == 1
    assert not console.errors


def test_reload_reports_restart_required_without_mutating_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_test_config(tmp_path)
    original_model = config.provider.model
    permission = PermissionGuard(PermissionMode.DEFAULT)
    console = _FakeConsole()

    new_config = make_test_config(tmp_path)
    new_config.provider = replace(new_config.provider, model="gpt-4o")

    monkeypatch.setattr("bareagent.main.load_config", lambda _path: new_config)

    _dispatch_reload_command(
        config=config,
        permission=permission,
        ui_console=cast(AgentConsole, console),
    )

    # Restart-required field is reported but the live config keeps the old value.
    assert config.provider.model == original_model
    assert any("requires restart" in msg for msg in console.status)


def test_reload_no_change_reports_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_test_config(tmp_path)
    permission = PermissionGuard(PermissionMode.DEFAULT)
    console = _FakeConsole()

    monkeypatch.setattr("bareagent.main.load_config", lambda _path: make_test_config(tmp_path))

    _dispatch_reload_command(
        config=config,
        permission=permission,
        ui_console=cast(AgentConsole, console),
    )

    assert any("unchanged" in msg.lower() for msg in console.status)
    assert not console.errors


def test_reload_invalid_theme_skips_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_theme("catppuccin-mocha")
    config = make_test_config(tmp_path)
    config.ui.theme = "catppuccin-mocha"
    permission = PermissionGuard(PermissionMode.DEFAULT)
    console = _FakeConsole()

    new_config = make_test_config(tmp_path)
    new_config.ui.theme = "no-such-theme"
    new_config.permission.mode = "auto"

    monkeypatch.setattr("bareagent.main.load_config", lambda _path: new_config)

    _dispatch_reload_command(
        config=config,
        permission=permission,
        ui_console=cast(AgentConsole, console),
    )

    # Bad theme is skipped (live theme unchanged) but the other hot field applies.
    assert config.ui.theme == "catppuccin-mocha"
    assert permission.mode == PermissionMode.AUTO
    assert console.errors  # unknown-theme error surfaced
