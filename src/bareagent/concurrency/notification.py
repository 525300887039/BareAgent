from __future__ import annotations

from typing import Any

from bareagent.concurrency.background import BackgroundManager
from bareagent.core.fileutil import is_tool_result_message, stringify

_NOTIFICATION_PREFIX = "<background-notifications>"


def inject_notifications(
    messages: list[dict[str, Any]],
    bg_manager: BackgroundManager,
) -> None:
    notifications = bg_manager.drain_notifications()
    if not notifications:
        return

    lines = ["后台任务更新："]
    surfaced = 0
    for notification in notifications:
        task_id = str(notification.get("task_id", "unknown"))
        # Workflow runs (task_id ``wf-<id>``) are delivered in full by the
        # dedicated _drain_workflow_results channel; skip them here so their
        # summary is not also injected truncated through this generic path.
        if task_id.startswith("wf-"):
            continue
        surfaced += 1
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

    # Every notification was a workflow run (delivered elsewhere) -> nothing to
    # inject here.
    if surfaced == 0:
        return

    notification_message = {
        "role": "system",
        "content": (
            f"{_NOTIFICATION_PREFIX}\n" + "\n".join(lines) + "\n</background-notifications>"
        ),
    }
    if messages and messages[-1].get("role") == "user":
        if is_tool_result_message(messages[-1]):
            messages.append(notification_message)
        else:
            messages.insert(len(messages) - 1, notification_message)
        return

    messages.append(notification_message)
