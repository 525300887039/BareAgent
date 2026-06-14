"""Tests for PermissionGuard MCP-tool integration (PR4).

Covers the four-mode behaviour for ``mcp__`` tools, DANGEROUS_PATTERNS
short-circuit, ``format_preview`` field truncation, and pass-through of
existing allow/deny prefix rules.
"""

from __future__ import annotations

import json

from bareagent.permission.guard import PermissionGuard, PermissionMode


def test_default_mode_mcp_tool_requires_confirm() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    assert guard.requires_confirm("mcp__fetch__fetch", {"url": "https://x"}) is True


def test_auto_mode_mcp_tool_auto_passes() -> None:
    guard = PermissionGuard(PermissionMode.AUTO)
    assert guard.requires_confirm("mcp__fetch__fetch", {"url": "https://x"}) is False


def test_plan_mode_mcp_tool_is_denied() -> None:
    """PLAN only allows SAFE_TOOLS; MCP tools have unknown side effects."""
    guard = PermissionGuard(PermissionMode.PLAN)
    assert guard.requires_confirm("mcp__fetch__fetch", {"url": "https://x"}) is True


def test_bypass_mode_mcp_tool_passes() -> None:
    guard = PermissionGuard(PermissionMode.BYPASS)
    assert guard.requires_confirm("mcp__fetch__fetch", {"url": "https://x"}) is False


def test_is_dangerous_skips_mcp_tools_even_with_shell_like_args() -> None:
    """MCP args are JSON, so shell DANGEROUS_PATTERNS must not be applied."""
    guard = PermissionGuard(PermissionMode.AUTO)
    assert guard.is_dangerous("mcp__shell__exec", {"cmd": "rm -rf /"}) is False


def test_is_dangerous_still_flags_bash_rm_rf() -> None:
    guard = PermissionGuard(PermissionMode.AUTO)
    assert guard.is_dangerous("bash", {"command": "rm -rf /"}) is True


def test_default_mcp_tool_with_rm_rf_args_still_only_asks() -> None:
    """A DEFAULT-mode MCP call with shell-like args should only ask, not deny."""
    guard = PermissionGuard(PermissionMode.DEFAULT)
    assert (
        guard.requires_confirm("mcp__shell__exec", {"cmd": "rm -rf /"}) is True
    )  # ask, not auto-deny


def test_format_preview_truncates_long_top_level_string() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    long_value = "x" * 300
    preview = guard.format_preview("mcp__x__y", {"text": long_value})
    parsed = json.loads(preview)
    assert parsed["text"].startswith("x" * 256)
    assert "... [truncated, 300 chars]" in parsed["text"]


def test_format_preview_keeps_short_values_and_supports_unicode() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    preview = guard.format_preview(
        "mcp__fs__write", {"path": "中文路径.txt", "size": 12}
    )
    # ensure_ascii=False keeps the original characters in the JSON output.
    assert "中文路径.txt" in preview
    parsed = json.loads(preview)
    assert parsed["size"] == 12
    assert parsed["path"] == "中文路径.txt"


def test_deny_prefix_rule_blocks_mcp_server_namespace() -> None:
    """``deny = ["mcp__github__"]`` should reject every github MCP tool."""
    guard = PermissionGuard(PermissionMode.AUTO)
    guard.deny_rules = ["mcp__github__create_issue(prefix:)"]
    # Subject for MCP tools is the JSON serialised args (no file_path / name).
    # Use a tool input with a recognised key so rule_subject picks it up.
    guard.deny_rules = ["mcp__github__create_issue(prefix:owner)"]
    assert (
        guard.requires_confirm(
            "mcp__github__create_issue",
            {"name": "owner/repo"},
        )
        is True
    )


def test_allow_prefix_rule_passes_mcp_call_in_default_mode() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    guard.allow_rules = ["mcp__fetch__fetch(prefix:https://trusted.example)"]
    assert (
        guard.requires_confirm(
            "mcp__fetch__fetch",
            {"name": "https://trusted.example/docs"},
        )
        is False
    )


def test_format_preview_handles_empty_input() -> None:
    guard = PermissionGuard(PermissionMode.DEFAULT)
    preview = guard.format_preview("mcp__x__y", {})
    assert json.loads(preview) == {}


def test_plan_mode_overrides_allow_rule_for_mcp_tools() -> None:
    """PLAN must stay strict even if a user has an allow rule for the tool."""
    guard = PermissionGuard(PermissionMode.PLAN)
    guard.allow_rules = ["mcp__fetch__fetch(prefix:https://trusted)"]
    assert (
        guard.requires_confirm(
            "mcp__fetch__fetch",
            {"name": "https://trusted/docs"},
        )
        is True
    )
