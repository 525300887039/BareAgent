"""Tests for ``AgentType.lsp_tools_enabled`` filter behaviour + tool registry
integration with ``lsp_manager``.
"""

from __future__ import annotations

from src.planning.agent_types import (
    BUILTIN_AGENT_TYPES,
    AgentType,
    filter_handlers,
    filter_tools,
)

_TOOLS = [
    {"name": "bash"},
    {"name": "read_file"},
    {"name": "subagent"},
    {"name": "lsp_outline"},
    {"name": "lsp_definition"},
    {"name": "lsp_references"},
    {"name": "lsp_diagnostics"},
    {"name": "semantic_rename"},
    {"name": "mcp__svc__do"},
]


def test_lsp_tools_enabled_default_true() -> None:
    """``AgentType()`` defaults preserve LSP tools."""
    custom = AgentType(name="custom", description="x")
    assert custom.lsp_tools_enabled is True


def test_lsp_disabled_strips_lsp_only() -> None:
    """When lsp_tools_enabled=False, only ``lsp_*`` tools are stripped — MCP
    tools and base tools remain untouched."""
    agent = AgentType(
        name="no-lsp",
        description="x",
        lsp_tools_enabled=False,
    )
    kept = {t["name"] for t in filter_tools(_TOOLS, agent)}
    assert "lsp_outline" not in kept
    assert "lsp_definition" not in kept
    assert "lsp_references" not in kept
    assert "lsp_diagnostics" not in kept
    # Non-LSP tools survive.
    assert "bash" in kept
    assert "mcp__svc__do" in kept


def test_lsp_enabled_mcp_disabled_are_independent() -> None:
    """The two flags must not influence each other: lsp_* survives, mcp__*
    is stripped."""
    agent = AgentType(
        name="mixed",
        description="x",
        mcp_tools_enabled=False,
        lsp_tools_enabled=True,
    )
    kept = {t["name"] for t in filter_tools(_TOOLS, agent)}
    assert "lsp_outline" in kept
    assert "lsp_definition" in kept
    assert "mcp__svc__do" not in kept


def test_read_only_defaults_keep_lsp() -> None:
    """The built-in read-only types (explore / plan / code-review) keep LSP
    tools since they are read-only by nature."""
    for name in ("explore", "plan", "code-review"):
        agent = BUILTIN_AGENT_TYPES[name]
        assert agent.lsp_tools_enabled is True
        # MCP tools are disabled by default for read-only types.
        assert agent.mcp_tools_enabled is False
        kept = {t["name"] for t in filter_tools(_TOOLS, agent)}
        assert "lsp_outline" in kept


def test_read_only_types_drop_semantic_rename() -> None:
    """semantic_rename is a write tool that does not carry the ``lsp_`` prefix,
    so ``lsp_tools_enabled=True`` must NOT keep it for read-only agents — it is
    denied via the explicit ``disallowed_tools`` entry instead."""
    for name in ("explore", "plan", "code-review"):
        agent = BUILTIN_AGENT_TYPES[name]
        # The read-only query tools survive...
        kept = {t["name"] for t in filter_tools(_TOOLS, agent)}
        assert "lsp_outline" in kept
        # ...but the write tool is stripped.
        assert "semantic_rename" not in kept
        assert agent.disallowed_tools is not None
        assert "semantic_rename" in agent.disallowed_tools


def test_filter_handlers_drops_stripped_lsp_handlers() -> None:
    agent = AgentType(
        name="no-lsp",
        description="x",
        lsp_tools_enabled=False,
    )
    filtered_tools = filter_tools(_TOOLS, agent)
    all_handlers = {
        "bash": lambda: None,
        "lsp_outline": lambda: None,
        "lsp_definition": lambda: None,
        "mcp__svc__do": lambda: None,
    }
    kept_handlers = filter_handlers(all_handlers, filtered_tools)
    assert "lsp_outline" not in kept_handlers
    assert "lsp_definition" not in kept_handlers
    assert "bash" in kept_handlers
    assert "mcp__svc__do" in kept_handlers
