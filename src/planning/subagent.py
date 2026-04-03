from __future__ import annotations

from typing import Any

from src.core.loop import agent_loop
from src.provider.base import BaseLLMProvider


SUBAGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "subagent",
        "description": "Delegate a self-contained task to a child agent with isolated messages.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to complete.",
                }
            },
            "required": ["task"],
        },
    }
]


def _subagent_compact(messages: list[dict[str, Any]], keep_recent: int = 40) -> None:
    """Trim early messages when the conversation grows too long, keeping system + recent."""
    if len(messages) <= keep_recent:
        return
    system_msgs = [m for m in messages if m.get("role") == "system"]
    recent = messages[-keep_recent:]
    messages.clear()
    messages.extend(system_msgs)
    system_ids = {id(m) for m in system_msgs}
    messages.extend(m for m in recent if id(m) not in system_ids)


def run_subagent(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str = "",
    max_depth: int = 3,
    current_depth: int = 0,
) -> str:
    if current_depth >= max_depth:
        return f"Subagent refused: recursion depth {current_depth} exceeds limit {max_depth}."
    messages: list[dict[str, Any]] = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": task})
    return agent_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        handlers=handlers,
        permission=permission,
        compact_fn=_subagent_compact,
    )
