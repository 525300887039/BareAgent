from __future__ import annotations

import json
from typing import Any

from src.concurrency.background import BackgroundManager

_NOTIFICATION_PREFIX = "<background-notifications>"


def inject_notifications(
    messages: list[dict[str, Any]],
    bg_manager: BackgroundManager,
) -> None:
    notifications = bg_manager.drain_notifications()
    if not notifications:
        return

    lines = ["后台任务更新："]
    for notification in notifications:
        task_id = str(notification.get("task_id", "unknown"))
        status = str(notification.get("status", "unknown"))
        detail = ""
        if "result" in notification:
            result_text = _stringify(notification["result"])
            if result_text:
                detail = f" - {result_text[:500]}"
        elif "error" in notification:
            error_text = _stringify(notification["error"])
            if error_text:
                detail = f" - {error_text[:500]}"
        lines.append(f"- Task {task_id}: {status}{detail}")

    notification_message = {
        "role": "system",
        "content": (
            f"{_NOTIFICATION_PREFIX}\n"
            + "\n".join(lines)
            + "\n</background-notifications>"
        ),
    }
    if messages and messages[-1].get("role") == "user":
        if _is_tool_result_message(messages[-1]):
            messages.append(notification_message)
        else:
            messages.insert(len(messages) - 1, notification_message)
        return

    messages.append(notification_message)


def _is_tool_result_message(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)
