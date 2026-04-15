"""三、权限系统验证 — 3.2 危险命令检测

适配：无 _is_dangerous 方法，改用 requires_confirm()。
DEFAULT 模式下 bash 命令：危险命令 → requires_confirm=True，安全命令可能也 True（因为 DEFAULT 对未匹配 bash 也需确认）。
改用 AUTO 模式：安全命令自动放行（False），危险命令仍需确认（True）。
"""

from src.permission.guard import PermissionGuard, PermissionMode

DANGEROUS_COMMANDS = [
    "rm -rf /",
    "rm -rf ~",
    "git push --force",
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
