"""三、权限系统验证 — 3.2 危险命令检测

适配：无 _is_dangerous 方法，改用 requires_confirm()。
DEFAULT 模式下 bash 命令：危险命令 → requires_confirm=True，
安全命令可能也 True（因为 DEFAULT 对未匹配 bash 也需确认）。
改用 AUTO 模式：安全命令自动放行（False），危险命令仍需确认（True）。
"""

import pytest

from bareagent.permission.guard import PermissionGuard, PermissionMode

DANGEROUS_COMMANDS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -fr build",
    "git push --force",
    "git push -f origin main",
    "git push -qf origin main",
    "GIT push --force origin main",
    "git clean -fdx",
    "Remove-Item -Recurse -Force build",
    "REMOVE-ITEM -FORCE -RECURSE build",
    "DROP TABLE users;",
    "chmod 777 /",
    "mkfs.ext4 /dev/sda",
]

SAFE_COMMANDS = [
    "ls -la",
    "git status",
    "pytest tests/",
    "cat README.md",
    "echo hello",
    "python --version",
    "git clean -n",
    "git clean -n target-f",
    "git push origin release-f",
    "Remove-Item -WhatIf build",
]

DESTRUCTIVE_GIT_VARIANTS = [
    "git.exe push --force",
    "git --no-pager push --force-with-lease=main",
    "git -C . clean -fdx",
    "git -c core.quotePath=false reset --hard",
    'git "push" "-qf" origin main',
    r'& "C:\Program Files\Git\cmd\git.exe" clean -xdf',
    '"/usr/bin/git" reset "--hard"',
    "git branch -D old-branch",
    "git.exe branch --delete old-branch",
]

READ_ONLY_GIT_VARIANTS = [
    "git.exe status --short",
    "git -C . status",
    "git --no-pager log -1",
    'git "show" HEAD',
    r'& "C:\Program Files\Git\cmd\git.exe" status',
    '"/usr/bin/git" diff --stat',
    "git branch",
    "git branch --list",
]

SAFE_GIT_NEIGHBORS = [
    "git.exe clean -n",
    "git -C . clean --dry-run -d",
    "git push origin release-f",
    'git "push" origin feature-fix',
    "git branch --list old-branch",
]


def test_dangerous_commands_detected():
    """所有危险命令在 AUTO 模式下应被标记为需确认"""
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    for cmd in DANGEROUS_COMMANDS:
        assert guard.requires_confirm("bash", {"command": cmd}) is True, (
            f"Should detect as dangerous: {cmd}"
        )


def test_safe_commands_not_flagged():
    """安全命令在 AUTO 模式下不应需要确认"""
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    for cmd in SAFE_COMMANDS:
        assert guard.requires_confirm("bash", {"command": cmd}) is False, (
            f"Should not require confirm: {cmd}"
        )


@pytest.mark.parametrize("command", DESTRUCTIVE_GIT_VARIANTS)
def test_destructive_git_invocation_variants_are_detected(command: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    tool_input = {"command": command}

    assert guard.requires_confirm("bash", tool_input) is True
    assert guard.is_dangerous("bash", tool_input) is True


@pytest.mark.parametrize("tool_name", ["bash", "background_run"])
def test_git_executable_variant_is_detected_for_shell_tools(tool_name: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    tool_input = {"command": "git.exe reset --hard"}

    assert guard.requires_confirm(tool_name, tool_input) is True
    assert guard.is_dangerous(tool_name, tool_input) is True


@pytest.mark.parametrize(
    ("command", "allow_rule"),
    [
        ("git.exe clean -fdx", "bash(prefix:git.exe clean)"),
        ("git -C . reset --hard", "bash(prefix:git -C .)"),
    ],
)
def test_allow_rule_cannot_bypass_destructive_git_variant(command: str, allow_rule: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)
    guard.allow_rules = [allow_rule]

    assert guard.requires_confirm("bash", {"command": command}) is True


def test_fail_closed_guard_denies_destructive_git_variant() -> None:
    guard = PermissionGuard(mode=PermissionMode.AUTO, fail_closed=True)
    tool_input = {"command": "git --no-pager push --force"}

    assert guard.requires_confirm("bash", tool_input) is True
    assert guard.ask_user(object()) is False


@pytest.mark.parametrize("command", READ_ONLY_GIT_VARIANTS)
def test_read_only_git_invocation_variants_remain_safe_in_default(command: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)
    tool_input = {"command": command}

    assert guard.requires_confirm("bash", tool_input) is False
    assert guard.is_dangerous("bash", tool_input) is False


@pytest.mark.parametrize("command", ["git branch new-branch", "git branch -m old new"])
def test_mutating_branch_forms_are_not_default_safe(command: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)

    assert guard.requires_confirm("bash", {"command": command}) is True


@pytest.mark.parametrize("command", SAFE_GIT_NEIGHBORS)
def test_safe_git_near_neighbors_are_not_dangerous(command: str) -> None:
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    tool_input = {"command": command}

    assert guard.requires_confirm("bash", tool_input) is False
    assert guard.is_dangerous("bash", tool_input) is False
