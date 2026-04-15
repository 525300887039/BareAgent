"""三、权限系统验证 — 3.1 PermissionGuard 模式测试

适配：构造函数接受 PermissionMode 枚举，方法是 requires_confirm()（True=需确认，语义与 check 相反）
"""

from src.permission.guard import PermissionGuard, PermissionMode


def test_bypass_mode_allows_all():
    """BYPASS 模式应允许所有操作（requires_confirm 返回 False）"""
    guard = PermissionGuard(mode=PermissionMode.BYPASS)
    assert guard.requires_confirm("bash", {"command": "rm -rf /"}) is False


def test_plan_mode_blocks_writes():
    """PLAN 模式应阻止写操作（requires_confirm 返回 True）"""
    guard = PermissionGuard(mode=PermissionMode.PLAN)
    result = guard.requires_confirm("write_file", {"path": "test.txt", "content": "x"})
    assert result is True


def test_plan_mode_allows_reads():
    """PLAN 模式应允许读操作（requires_confirm 返回 False）"""
    guard = PermissionGuard(mode=PermissionMode.PLAN)
    result = guard.requires_confirm("read_file", {"path": "test.txt"})
    assert result is False


def test_plan_mode_allows_glob():
    """PLAN 模式应允许 glob"""
    guard = PermissionGuard(mode=PermissionMode.PLAN)
    result = guard.requires_confirm("glob", {"pattern": "*.py"})
    assert result is False
