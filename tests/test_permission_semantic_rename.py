"""Permission semantics for the ``semantic_rename`` write tool.

``semantic_rename`` mutates files across the workspace, so it must behave like
``write_file``: confirm in DEFAULT, auto-approve in AUTO, reject in PLAN, and
pass through BYPASS. It is deliberately NOT in ``SAFE_TOOLS``.
"""

from __future__ import annotations

from bareagent.permission.guard import PermissionGuard, PermissionMode

_INPUT = {"file": "mod.py", "line": 1, "col": 1, "new_name": "bar"}


def test_semantic_rename_not_in_safe_tools() -> None:
    assert "semantic_rename" not in PermissionGuard.SAFE_TOOLS


def test_semantic_rename_default_requires_confirm() -> None:
    guard = PermissionGuard(mode=PermissionMode.DEFAULT)
    assert guard.requires_confirm("semantic_rename", _INPUT) is True


def test_semantic_rename_auto_passes() -> None:
    guard = PermissionGuard(mode=PermissionMode.AUTO)
    assert guard.requires_confirm("semantic_rename", _INPUT) is False


def test_semantic_rename_plan_rejects() -> None:
    guard = PermissionGuard(mode=PermissionMode.PLAN)
    # PLAN denies every tool not in SAFE_TOOLS regardless of allow rules.
    assert guard.requires_confirm("semantic_rename", _INPUT) is True


def test_semantic_rename_bypass_skips() -> None:
    guard = PermissionGuard(mode=PermissionMode.BYPASS)
    assert guard.requires_confirm("semantic_rename", _INPUT) is False
