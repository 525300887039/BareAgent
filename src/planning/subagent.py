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


def run_subagent(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str = "",
) -> str:
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
        compact_fn=lambda _messages: None,
    )
