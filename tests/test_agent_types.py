from __future__ import annotations

from bareagent.permission.guard import PermissionGuard, PermissionMode
from bareagent.planning.agent_types import (
    BUILTIN_AGENT_TYPES,
    DEFAULT_AGENT_TYPE,
    AgentType,
    filter_handlers,
    filter_tools,
    resolve_agent_type,
)


def test_resolve_agent_type_uses_configured_default_and_fallback() -> None:
    assert resolve_agent_type(None, default_name="plan").name == "plan"
    assert resolve_agent_type("missing", default_name="plan").name == "plan"
    assert resolve_agent_type("missing", default_name="unknown").name == DEFAULT_AGENT_TYPE


def test_filter_tools_applies_blacklist_and_nesting_rules() -> None:
    all_tools = [
        {"name": "read_file"},
        {"name": "write_file"},
        {"name": "bash"},
        {"name": "subagent"},
        {"name": "todo_write"},
    ]

    filtered = filter_tools(all_tools, BUILTIN_AGENT_TYPES["explore"])

    assert [tool["name"] for tool in filtered] == ["read_file", "todo_write"]


def test_filter_tools_applies_whitelist_before_blacklist() -> None:
    custom = AgentType(
        name="custom",
        description="test",
        tools=["read_file", "bash", "subagent"],
        disallowed_tools=["bash"],
        allow_nesting=False,
    )

    filtered = filter_tools(
        [
            {"name": "read_file"},
            {"name": "bash"},
            {"name": "subagent"},
            {"name": "write_file"},
        ],
        custom,
    )

    assert [tool["name"] for tool in filtered] == ["read_file"]


def test_filter_handlers_matches_filtered_tool_names() -> None:
    handlers = {
        "read_file": object(),
        "write_file": object(),
        "subagent": object(),
    }

    filtered = filter_handlers(
        handlers,
        [{"name": "read_file"}, {"name": "subagent"}],
    )

    assert set(filtered) == {"read_file", "subagent"}


def test_permission_guard_for_subagent_copies_rules_and_applies_plan_mode() -> None:
    parent = PermissionGuard(PermissionMode.DEFAULT)
    parent.allow_rules = ["Bash(prefix:pytest*)"]
    parent.deny_rules = ["Bash(prefix:rm*)"]

    child = parent.for_subagent(BUILTIN_AGENT_TYPES["code-review"])

    assert child.mode == PermissionMode.PLAN
    assert child.fail_closed is True
    assert child.allow_rules == parent.allow_rules
    assert child.deny_rules == parent.deny_rules


def test_permission_guard_for_background_subagent_fails_closed() -> None:
    parent = PermissionGuard(PermissionMode.DEFAULT)

    child = parent.for_subagent(BUILTIN_AGENT_TYPES["general-purpose"], background=True)

    assert child.mode == PermissionMode.DEFAULT
    assert child.fail_closed is True


def test_custom_agent_type_defaults_to_mcp_enabled() -> None:
    """Backwards compatibility: user-defined types without the field stay True."""
    custom = AgentType(name="custom", description="test")
    assert custom.mcp_tools_enabled is True


def test_read_only_builtins_disable_mcp_tools() -> None:
    for type_name in ("explore", "plan", "code-review"):
        assert BUILTIN_AGENT_TYPES[type_name].mcp_tools_enabled is False, type_name


def test_general_purpose_keeps_mcp_tools_enabled() -> None:
    assert BUILTIN_AGENT_TYPES["general-purpose"].mcp_tools_enabled is True


def test_filter_tools_strips_mcp_tools_for_explore() -> None:
    all_tools = [
        {"name": "mcp__fetch__fetch"},
        {"name": "mcp__github__create_issue"},
        {"name": "read_file"},
    ]
    filtered = filter_tools(all_tools, BUILTIN_AGENT_TYPES["explore"])
    assert [tool["name"] for tool in filtered] == ["read_file"]


def test_filter_tools_keeps_mcp_tools_for_general_purpose() -> None:
    all_tools = [
        {"name": "mcp__fetch__fetch"},
        {"name": "read_file"},
    ]
    filtered = filter_tools(all_tools, BUILTIN_AGENT_TYPES["general-purpose"])
    assert {tool["name"] for tool in filtered} == {
        "mcp__fetch__fetch",
        "read_file",
    }
