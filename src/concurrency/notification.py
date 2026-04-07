from __future__ import annotations

from typing import Any

from src.concurrency.background import BackgroundManager
from src.core.fileutil import is_tool_result_message, stringify

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
            result_text = stringify(notification["result"])
            if result_text:
                detail = f" - {result_text[:500]}"
        elif "error" in notification:
            error_text = stringify(notification["error"])
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
        if is_tool_result_message(messages[-1]):
            messages.append(notification_message)
        else:
            messages.insert(len(messages) - 1, notification_message)
        return

    messages.append(notification_message)
