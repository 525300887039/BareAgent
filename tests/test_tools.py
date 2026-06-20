from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bareagent.core.handlers.bash import run_bash
from bareagent.core.handlers.file_edit import run_edit
from bareagent.core.handlers.file_read import run_read
from bareagent.core.handlers.file_write import run_write
from bareagent.core.handlers.glob_search import run_glob
from bareagent.core.handlers.grep_search import run_grep
from bareagent.core.sandbox import safe_path
from bareagent.core.tools import get_handlers, get_tools, tool_search
from bareagent.permission.guard import PermissionGuard, PermissionMode
from bareagent.permission.rules import parse_permission_rules
from bareagent.provider.base import ToolCall


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


def test_glob_and_grep_skip_ignored_trees_but_allow_explicit_paths(
    tmp_path: Path,
) -> None:
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

    # dangerous patterns override allow rules (fail-closed)
    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is True
    # non-dangerous allowed command passes
    assert guard.requires_confirm("bash", {"command": "rm temp.log"}) is False
    assert guard.requires_confirm("bash", {"command": "npm publish"}) is True


def test_permission_guard_auto_mode_allows_safe_patterns() -> None:
    guard = PermissionGuard(PermissionMode.AUTO)

    assert guard.requires_confirm("bash", {"command": "pytest tests/test_tools.py"}) is False
    assert guard.requires_confirm("bash", {"command": "rm -rf build"}) is True


def test_permission_guard_plan_mode_blocks_write_operations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard = PermissionGuard(PermissionMode.PLAN)
    call = ToolCall(id="toolu_1", name="write_file", input={"file_path": "out.txt", "content": "x"})

    assert guard.requires_confirm("write_file", {"file_path": "out.txt", "content": "x"}) is True
    assert guard.requires_confirm("task_create", {"title": "plan only"}) is True
    assert guard.requires_confirm("task_update", {"task_id": "abc12345", "status": "done"}) is True
    assert (
        guard.requires_confirm("team_send", {"to_agent": "reviewer", "content": "write code"})
        is True
    )
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


def test_permission_guard_write_file_honors_allow_rules() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    guard.allow_rules = ["write_file(prefix:notes/today.txt*)"]

    assert (
        guard.requires_confirm(
            "write_file",
            {"file_path": "notes/today.txt", "content": "hello"},
        )
        is False
    )
    assert (
        guard.requires_confirm(
            "write_file",
            {"file_path": "notes/tomorrow.txt", "content": "hello"},
        )
        is True
    )


def test_permission_guard_requires_confirmation_for_unknown_non_safe_tools() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)

    assert guard.requires_confirm("subagent", {"task": "inspect repo"}) is True


def test_subagent_schema_exposes_agent_type_and_background_flag() -> None:
    schema = next(tool for tool in get_tools() if tool["name"] == "subagent")
    properties = schema["parameters"]["properties"]

    assert "agent_type" in properties
    assert "run_in_background" in properties


def test_get_handlers_subagent_forwards_extended_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run_subagent(**kwargs):
        captured.update(kwargs)
        return "delegated"

    monkeypatch.setattr("bareagent.core.tools.run_subagent", _fake_run_subagent)
    permission = PermissionGuard(PermissionMode.DEFAULT)
    bg_manager = object()
    tools = [{"name": "subagent", "parameters": {"type": "object", "properties": {}}}]
    handlers = get_handlers(
        tmp_path,
        provider=object(),
        tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        subagent_system_prompt="system prompt",
        subagent_max_depth=5,
        subagent_default_type="plan",
    )

    result = handlers["subagent"](
        task="Inspect the repo",
        agent_type="explore",
        run_in_background=True,
    )

    assert result == "delegated"
    assert captured["task"] == "Inspect the repo"
    assert captured["agent_type"] == "explore"
    assert captured["run_in_background"] is True
    assert captured["system_prompt"] == "system prompt"
    assert captured["max_depth"] == 5
    assert captured["default_agent_type"] == "plan"
    assert captured["bg_manager"] is bg_manager


def test_bash_handler_runs_in_bound_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    other_dir = tmp_path / "other"
    workspace.mkdir()
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    command = "Get-Location | Select-Object -ExpandProperty Path" if os.name == "nt" else "pwd"

    output = get_handlers(workspace)["bash"](command)

    assert str(workspace.resolve()) in output
    assert str(other_dir.resolve()) not in output


def test_bash_handler_decodes_binary_output_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        _ = args, kwargs
        return SimpleNamespace(stdout="ok\ufffd", stderr="", returncode=0)

    monkeypatch.setattr("bareagent.core.handlers.bash.subprocess.run", fake_run)

    assert run_bash("echo ok") == "ok\ufffd"


def test_bash_handler_argv_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        _ = kwargs
        captured["args"] = args
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("bareagent.core.handlers.bash.subprocess.run", fake_run)

    monkeypatch.setattr("bareagent.core.handlers.bash.os.name", "nt")
    run_bash("Write-Output hi")
    win_args = captured["args"]
    assert win_args[:3] == ["powershell", "-NoProfile", "-Command"]
    assert "[Console]::OutputEncoding" in win_args[3]
    assert "[System.Text.Encoding]::UTF8" in win_args[3]
    assert win_args[3].endswith("Write-Output hi")

    monkeypatch.setattr("bareagent.core.handlers.bash.os.name", "posix")
    run_bash("echo hi")
    posix_args = captured["args"]
    assert posix_args == ["bash", "-lc", "echo hi"]
    assert not any("[Console]::OutputEncoding" in part for part in posix_args)


@pytest.mark.skipif(os.name != "nt", reason="round-trip exercises the real Windows PowerShell path")
def test_bash_handler_windows_chinese_output_round_trip() -> None:
    output = run_bash('Write-Output "\u4e2d\u6587\u6d4b\u8bd5"')

    assert "\u4e2d\u6587\u6d4b\u8bd5" in output
    assert "\ufffd" not in output


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


def test_glob_simple_pattern_recurses_into_subdirectories(tmp_path: Path) -> None:
    """Bug #13: *.py should find files in nested subdirectories."""
    (tmp_path / "top.py").write_text("top\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("deep\n", encoding="utf-8")
    (tmp_path / "sub" / "nested").mkdir()
    (tmp_path / "sub" / "nested" / "leaf.py").write_text("leaf\n", encoding="utf-8")

    result = run_glob("*.py", workspace=tmp_path)

    assert "top.py" in result
    assert "sub/deep.py" in result
    assert "sub/nested/leaf.py" in result


def test_importing_tools_does_not_parse_tasks_file_on_module_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bareagent.core.tools as tools_module

    (tmp_path / ".tasks.json").write_text("{not-valid-json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    reloaded = importlib.reload(tools_module)

    assert "task_list" in reloaded.TOOL_HANDLERS


# -- code_search tool surface (task 06-19) ---------------------------------


class _FakeCodeEmbedder:
    identity = "fake:v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if "authenticate" in t.lower() else 0.0, 0.1] for t in texts]


def _make_code_index(workspace: Path):
    from bareagent.memory.code_index import CodeIndex

    return CodeIndex(
        workspace,
        embedder=_FakeCodeEmbedder(),
        cache_path=workspace / ".code-index.json",
    )


def test_code_search_is_in_deferred_and_safe_not_in_readonly_blacklist() -> None:
    from bareagent.core.tools import DEFERRED_TOOLS
    from bareagent.permission.guard import PermissionGuard
    from bareagent.planning.agent_types import _READ_ONLY_DEFAULTS

    assert "code_search" in DEFERRED_TOOLS
    assert "code_search" in PermissionGuard.SAFE_TOOLS
    # Like grep, code_search is read-only and must NOT be denied to the
    # read-only sub-agent types (explore / plan / code-review).
    assert "code_search" not in _READ_ONLY_DEFAULTS["disallowed_tools"]


def test_code_search_schema_gated_on_code_index() -> None:
    # Without a CodeIndex the tool is withheld (no dead tool exposed).
    names_off = {t["name"] for t in get_tools()}
    assert "code_search" not in names_off


def test_code_search_schema_present_when_code_index_wired(tmp_path: Path) -> None:
    index = _make_code_index(tmp_path)
    schema = next(t for t in get_tools(code_index=index) if t["name"] == "code_search")
    properties = schema["parameters"]["properties"]
    assert "query" in properties
    assert "k" in properties
    assert "path" in properties


def test_code_search_explore_subagent_sees_the_tool(tmp_path: Path) -> None:
    from bareagent.planning.agent_types import filter_tools, resolve_agent_type

    index = _make_code_index(tmp_path)
    tools = get_tools(code_index=index)
    explore = resolve_agent_type("explore")
    visible = {t["name"] for t in filter_tools(tools, explore)}
    # Read-only agents keep code_search (same as grep); but not write tools.
    assert "code_search" in visible
    assert "write_file" not in visible


def test_code_search_handler_returns_formatted_hits(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text(
        "def authenticate(user):\n    return user\n", encoding="utf-8"
    )
    index = _make_code_index(tmp_path)
    handlers = get_handlers(tmp_path, code_index=index)
    assert "code_search" in handlers
    result = handlers["code_search"](query="how to authenticate a user", k=5)
    assert "auth.py:1-2" in result
    assert "def authenticate" in result


def test_code_search_handler_absent_without_index(tmp_path: Path) -> None:
    handlers = get_handlers(tmp_path)
    assert "code_search" not in handlers


def test_code_search_handler_no_results_points_at_grep(tmp_path: Path) -> None:
    # Empty workspace -> no chunks -> friendly note steering to grep.
    index = _make_code_index(tmp_path)
    handlers = get_handlers(tmp_path, code_index=index)
    result = handlers["code_search"](query="nonexistent thing", k=5)
    assert "grep" in result.lower()
