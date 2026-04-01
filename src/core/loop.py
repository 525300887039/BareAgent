from __future__ import annotations

import json
from typing import Any, Callable

from src.provider.base import BaseLLMProvider, ToolCall


def agent_loop(
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    handlers: dict[str, Callable[..., Any]],
    permission: Any = None,
    compact_fn: Callable[[list[dict[str, Any]]], None] | None = None,
    bg_manager: Any = None,
) -> str:
    compact = compact_fn or (lambda _messages: None)

    while True:
        _run_background(bg_manager, messages)
        compact(messages)

        try:
            response = provider.create(messages=messages, tools=tools)
        except Exception as exc:
            return f"LLM call failed: {type(exc).__name__}: {exc}"

        messages.append(response.to_message())
        if not response.has_tool_calls:
            return response.text

        results: list[dict[str, Any]] = []
        for call in response.tool_calls:
            if _requires_confirmation(permission, call):
                if not _ask_permission(permission, call):
                    results.append(_tool_result(call.id, "User denied.", is_error=True))
                    continue

            handler = handlers.get(call.name)
            if handler is None:
                results.append(
                    _tool_result(call.id, f"Unknown tool: {call.name}", is_error=True)
                )
                continue

            try:
                output = handler(**call.input)
            except Exception as exc:
                results.append(
                    _tool_result(
                        call.id,
                        f"Error: {type(exc).__name__}: {exc}",
                        is_error=True,
                    )
                )
                continue

            results.append(_tool_result(call.id, output))

        messages.append({"role": "user", "content": results})


def _tool_result(tool_use_id: str, output: Any, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": _stringify_output(output),
    }
    if is_error:
        result["is_error"] = True
    return result


def _stringify_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False, default=str)


def _run_background(bg_manager: Any, messages: list[dict[str, Any]]) -> None:
    if bg_manager is None:
        return
    if callable(bg_manager):
        bg_manager(messages)
        return

    inject_notifications = getattr(bg_manager, "inject_notifications", None)
    if callable(inject_notifications):
        inject_notifications(messages)


def _requires_confirmation(permission: Any, call: ToolCall) -> bool:
    if permission is None:
        return False

    requires_confirm = getattr(permission, "requires_confirm", None)
    if callable(requires_confirm):
        return bool(requires_confirm(call.name, call.input))
    return False


def _ask_permission(permission: Any, call: ToolCall) -> bool:
    ask_user = getattr(permission, "ask_user", None)
    if callable(ask_user):
        return bool(ask_user(call))
    return False
