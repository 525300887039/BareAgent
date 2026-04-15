from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.permission.guard import PermissionMode

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentType:
    """Definition for a built-in child-agent profile."""

    name: str
    description: str
    system_prompt: str = ""
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    max_turns: int = 200
    allow_nesting: bool = True
    permission_mode: PermissionMode | None = None


_READ_ONLY_DEFAULTS: dict[str, Any] = {
    "disallowed_tools": ["write_file", "edit_file", "bash", "subagent"],
    "max_turns": 50,
    "allow_nesting": False,
    "permission_mode": PermissionMode.PLAN,
}

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {
    "general-purpose": AgentType(
        name="general-purpose",
        description="General child agent with the full inherited toolset.",
    ),
    "explore": AgentType(
        name="explore",
        description="Read-only agent for code search and repository understanding.",
        system_prompt=(
            "You are a read-only exploration agent. Search and inspect the repository, "
            "but do not modify files or perform side effects."
        ),
        **_READ_ONLY_DEFAULTS,
    ),
    "plan": AgentType(
        name="plan",
        description="Planning agent for implementation design without repository mutation.",
        system_prompt=(
            "You are a planning agent. Analyze the codebase and produce an implementation "
            "plan, but do not modify files or perform side effects."
        ),
        **_READ_ONLY_DEFAULTS,
    ),
    "code-review": AgentType(
        name="code-review",
        description="Read-only review agent for bugs, regressions, and code quality issues.",
        system_prompt=(
            "You are a code review agent. Inspect code for defects, regressions, security "
            "issues, and maintainability risks. Do not modify files."
        ),
        **_READ_ONLY_DEFAULTS,
    ),
}

DEFAULT_AGENT_TYPE = "general-purpose"


def resolve_agent_type(
    name: str | None,
    *,
    default_name: str = DEFAULT_AGENT_TYPE,
) -> AgentType:
    """Resolve a child-agent type, falling back to the configured default."""

    resolved_default = BUILTIN_AGENT_TYPES.get(
        default_name, BUILTIN_AGENT_TYPES[DEFAULT_AGENT_TYPE]
    )
    if name is None:
        return resolved_default
    if name not in BUILTIN_AGENT_TYPES:
        _log.warning(
            "Unknown agent type %r, falling back to %r", name, resolved_default.name
        )
        return resolved_default
    return BUILTIN_AGENT_TYPES[name]


def filter_tools(
    all_tools: list[dict[str, Any]],
    agent_type: AgentType,
) -> list[dict[str, Any]]:
    """Apply whitelist, blacklist, and nesting controls to a tool schema list."""

    allowed = set(agent_type.tools) if agent_type.tools is not None else None
    denied = (
        set(agent_type.disallowed_tools)
        if agent_type.disallowed_tools is not None
        else None
    )
    strip_nesting = not agent_type.allow_nesting

    def _keep(tool: dict[str, Any]) -> bool:
        name = str(tool.get("name"))
        if allowed is not None and name not in allowed:
            return False
        if denied is not None and name in denied:
            return False
        if strip_nesting and name == "subagent":
            return False
        return True

    return [tool for tool in all_tools if _keep(tool)]


def filter_handlers(
    all_handlers: dict[str, Any],
    filtered_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep only handlers that still have a matching tool schema."""

    allowed_names = {str(tool.get("name")) for tool in filtered_tools}
    return {
        name: handler for name, handler in all_handlers.items() if name in allowed_names
    }
