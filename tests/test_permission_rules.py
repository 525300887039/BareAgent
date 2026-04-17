"""三、权限系统验证 — 3.3 Allow/Deny 规则

适配：构造函数无 allow/deny 参数，需构造后赋值 .allow_rules / .deny_rules。
AUTO 模式下 bash 命令默认放行（除非匹配 deny 或 dangerous）。
"""

import src.main as main_module
from src.permission.guard import PermissionGuard, PermissionMode


def test_allow_rule_prefix():
    """allow 前缀规则应自动通过（requires_confirm=False）"""
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)
    guard.allow_rules = ["bash(prefix:git status)"]
    result = guard.requires_confirm("bash", {"command": "git status"})
    assert result is False


def test_deny_rule_prefix():
    """deny 前缀规则应阻止（requires_confirm=True）"""
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    guard.deny_rules = ["bash(prefix:git push)"]
    result = guard.requires_confirm("bash", {"command": "git push origin main"})
    assert result is True


def test_multiline_allow_rule_matches_tool_input() -> None:
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)
    rule = main_module._build_permission_allow_rule(
        "bash",
        {"command": "git status\npython -m pytest"},
    )

    assert rule is not None
    guard.allow_rules = [rule]
    assert (
        guard.requires_confirm(
            "bash",
            {"command": "git status\npython -m pytest"},
        )
        is False
    )
