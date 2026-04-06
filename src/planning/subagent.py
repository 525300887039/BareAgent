from __future__ import annotations

from functools import partial
from typing import Any

from src.concurrency.background import BackgroundManager
from src.core.fileutil import generate_random_id
from src.core.loop import agent_loop
from src.memory.compact import Compactor
from src.permission.guard import PermissionGuard
from src.planning.agent_types import (
    BUILTIN_AGENT_TYPES,
    DEFAULT_AGENT_TYPE,
    AgentType,
    filter_handlers,
    filter_tools,
    resolve_agent_type,
)
from src.provider.base import BaseLLMProvider

_SUBAGENT_COMPACT_THRESHOLD = 50_000


def _build_subagent_description() -> str:
    lines = ["Delegate a self-contained task to a child agent with isolated messages.", "Available agent types:"]
    for name, at in BUILTIN_AGENT_TYPES.items():
        lines.append(f"- {name}: {at.description}")
    return "\n".join(lines)


SUBAGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "subagent",
        "description": _build_subagent_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to complete.",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Optional child-agent profile to use.",
                    "enum": list(BUILTIN_AGENT_TYPES.keys()),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run the child agent asynchronously in the background.",
                    "default": False,
                },
            },
            "required": ["task"],
        },
    }
]


def run_subagent(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str = "",
    max_depth: int = 3,
    current_depth: int = 1,
    agent_type: str | None = None,
    bg_manager: BackgroundManager | None = None,
    run_in_background: bool = False,
    default_agent_type: str = DEFAULT_AGENT_TYPE,
) -> str:
    if current_depth > max_depth:
        return f"Subagent refused: recursion depth {current_depth} exceeds limit {max_depth}."

    resolved_type = resolve_agent_type(agent_type, default_name=default_agent_type)
    child_permission = _build_child_permission(
        permission=permission,
        agent_type=resolved_type,
        background=run_in_background,
    )

    if run_in_background:
        if bg_manager is None:
            return "Subagent background execution unavailable: background manager is not configured."
        task_id = _generate_subagent_task_id()
        bg_manager.submit(
            task_id,
            partial(
                _run_subagent_sync,
                provider=provider,
                task=task,
                tools=tools,
                handlers=handlers,
                permission=child_permission,
                system_prompt=system_prompt,
                max_depth=max_depth,
                current_depth=current_depth,
                resolved_type=resolved_type,
                bg_manager=bg_manager,
                default_agent_type=default_agent_type,
            ),
        )
        return f"Subagent {task_id} started in the background."

    return _run_subagent_sync(
        provider=provider,
        task=task,
        tools=tools,
        handlers=handlers,
        permission=child_permission,
        system_prompt=system_prompt,
        max_depth=max_depth,
        current_depth=current_depth,
        resolved_type=resolved_type,
        bg_manager=bg_manager,
        default_agent_type=default_agent_type,
    )


def _run_subagent_sync(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str,
    max_depth: int,
    current_depth: int,
    resolved_type: AgentType,
    bg_manager: BackgroundManager | None,
    default_agent_type: str,
) -> str:
    filtered_tools = filter_tools(tools, resolved_type)
    child_handlers = filter_handlers(handlers, filtered_tools)
    resolved_system_prompt = _compose_system_prompt(
        parent_prompt=system_prompt,
        agent_prompt=resolved_type.system_prompt,
    )
    compact_fn = Compactor(
        provider=provider,
        transcript_mgr=None,
        threshold=_SUBAGENT_COMPACT_THRESHOLD,
    )

    if "subagent" in child_handlers:
        child_handlers["subagent"] = (
            lambda task, agent_type=None, run_in_background=False: run_subagent(
                provider=provider,
                task=task,
                tools=filtered_tools,
                handlers=child_handlers,
                permission=permission,
                system_prompt=resolved_system_prompt,
                max_depth=max_depth,
                current_depth=current_depth + 1,
                agent_type=agent_type,
                bg_manager=bg_manager,
                run_in_background=run_in_background,
                default_agent_type=default_agent_type,
            )
        )

    messages: list[dict[str, Any]] = []
    if resolved_system_prompt.strip():
        messages.append({"role": "system", "content": resolved_system_prompt})
    messages.append({"role": "user", "content": task})
    return agent_loop(
        provider=provider,
        messages=messages,
        tools=filtered_tools,
        handlers=child_handlers,
        permission=permission,
        compact_fn=compact_fn,
        bg_manager=None,
        max_iterations=resolved_type.max_turns,
    )


def _compose_system_prompt(*, parent_prompt: str, agent_prompt: str) -> str:
    parts = [p for raw in (parent_prompt, agent_prompt) if (p := raw.strip())]
    return "\n\n".join(parts)


def _build_child_permission(
    *,
    permission: Any,
    agent_type: AgentType,
    background: bool,
) -> Any:
    if isinstance(permission, PermissionGuard):
        return permission.for_subagent(agent_type, background=background)
    return permission


def _generate_subagent_task_id() -> str:
    return "subagent-" + generate_random_id(8)
