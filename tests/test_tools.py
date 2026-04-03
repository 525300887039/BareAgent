from __future__ import annotations

import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.core.handlers.bash import run_bash
from src.core.handlers.file_edit import run_edit
from src.core.handlers.file_read import run_read
from src.core.handlers.file_write import run_write
from src.core.handlers.glob_search import run_glob
from src.core.handlers.grep_search import run_grep
from src.core.sandbox import safe_path
from src.core.tools import get_handlers, tool_search
from src.permission.guard import PermissionGuard, PermissionMode
from src.permission.rules import parse_permission_rules
from src.provider.base import ToolCall


def test_safe_path_blocks_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        safe_path("../outside.txt", tmp_path)


def test_file_write_and_read_round_trip(tmp_path: Path) -> None:
    message = run_write("nested/example.txt", "alpha\nbeta\n", workspace=tmp_path)

    assert message == "Wrote 11 characters to nested/example.txt"
    assert run_read("nested/example.txt", workspace=tmp_path) == "1: alpha\n2: beta"
    assert run_read("nested/example.txt", offset=1, limit=1, workspace=tmp_path) == "2: beta"


def test_file_edit_replaces_text(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world", encoding="utf-8")

    message = run_edit("sample.txt", "world", "there", workspace=tmp_path)

    assert message == "Edited sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello there"


def test_file_edit_raises_when_old_text_missing(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hello world", encoding="utf-8")

    with pytest.raises(ValueError, match="old_text not found"):
        run_edit("sample.txt", "missing", "there", workspace=tmp_path)


def test_glob_and_grep_search(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('needle')\n", encoding="utf-8")
    (tmp_path / "src" / "helper.txt").write_text("needle line\n", encoding="utf-8")

    assert run_glob("**/*.py", workspace=tmp_path) == ["src/main.py"]
    assert run_grep("needle", workspace=tmp_path) == [
        "src/helper.txt:1:needle line",
        "src/main.py:1:print('needle')",
    ]
    assert run_grep("needle", include="**/*.py", workspace=tmp_path) == [
        "src/main.py:1:print('needle')"
    ]


def test_glob_and_grep_skip_ignored_trees_but_allow_explicit_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "src" / "main.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".git" / "hooks.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".pytest_cache" / "cache.py").write_text("needle\n", encoding="utf-8")

    assert run_glob("**/*.py", workspace=tmp_path) == ["src/main.py"]
    assert run_grep("needle", workspace=tmp_path) == ["src/main.py:1:needle"]
    assert run_glob("**/*.py", path=".venv", workspace=tmp_path) == [".venv/lib.py"]
    assert run_grep("needle", path=".venv", workspace=tmp_path) == [".venv/lib.py:1:needle"]


def test_permission_guard_default_mode_for_safe_and_dangerous_tools() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)

    assert guard.requires_confirm("read_file", {"file_path": "config.toml"}) is False
    assert guard.requires_confirm("load_skill", {"skill_name": "git"}) is False
    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is True
    assert guard.requires_confirm("bash", {"command": "git status"}) is False


def test_permission_guard_default_mode_honors_allow_and_deny_rules() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    guard.allow_rules = ["Bash(prefix:rm*)"]
    guard.deny_rules = ["Bash(prefix:npm publish*)"]

    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is False
    assert guard.requires_confirm("bash", {"command": "npm publish"}) is True


def test_permission_guard_auto_mode_allows_safe_patterns() -> None:
    guard = PermissionGuard(PermissionMode.AUTO)

    assert guard.requires_confirm("bash", {"command": "pytest tests/test_tools.py"}) is False
    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is True


def test_permission_guard_plan_mode_blocks_write_operations(capsys: pytest.CaptureFixture[str]) -> None:
    guard = PermissionGuard(PermissionMode.PLAN)
    call = ToolCall(id="toolu_1", name="write_file", input={"file_path": "out.txt", "content": "x"})

    assert guard.requires_confirm("write_file", {"file_path": "out.txt", "content": "x"}) is True
    assert guard.requires_confirm("load_skill", {"skill_name": "git"}) is False
    assert guard.ask_user(call) is False
    assert "Plan mode: write_file blocked (read-only)" in capsys.readouterr().out


def test_permission_guard_bypass_mode_allows_everything() -> None:
    guard = PermissionGuard(PermissionMode.BYPASS)

    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is False
    assert guard.requires_confirm("write_file", {"file_path": "x", "content": "y"}) is False


def test_permission_rules_prefix_matching() -> None:
    guard = PermissionGuard(PermissionMode.AUTO)
    guard.allow_rules = ["Bash(prefix:npm*)"]
    guard.deny_rules = ["Bash(prefix:npm publish*)"]

    assert guard.requires_confirm("bash", {"command": "npm install"}) is False
    assert guard.requires_confirm("bash", {"command": "npm publish"}) is True


def test_permission_guard_requires_confirmation_for_unknown_non_safe_tools() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)

    assert guard.requires_confirm("subagent", {"task": "inspect repo"}) is True


def test_bash_handler_runs_in_bound_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    other_dir = tmp_path / "other"
    workspace.mkdir()
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    command = (
        "Get-Location | Select-Object -ExpandProperty Path"
        if os.name == "nt"
        else "pwd"
    )

    output = get_handlers(workspace)["bash"](command)

    assert str(workspace.resolve()) in output
    assert str(other_dir.resolve()) not in output


def test_bash_handler_decodes_binary_output_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        _ = args, kwargs
        return SimpleNamespace(stdout=b"ok\xae", stderr=b"", returncode=0)

    monkeypatch.setattr("src.core.handlers.bash.subprocess.run", fake_run)

    assert run_bash("echo ok") == "ok\ufffd"


def test_parse_permission_rules_reads_allow_and_deny_lists() -> None:
    allow, deny = parse_permission_rules(
        {
            "permission": {
                "allow": ["Bash(prefix:npm*)"],
                "deny": ["Bash(prefix:rm*)"],
            }
        }
    )

    assert allow == ["Bash(prefix:npm*)"]
    assert deny == ["Bash(prefix:rm*)"]


def test_tool_search_placeholder_returns_empty_list() -> None:
    assert tool_search("todo", max_results=3) == []
