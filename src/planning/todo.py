from __future__ import annotations

import threading
from typing import Any

from src.core.schema import tool_schema as _schema


VALID_TODO_STATUSES = {"pending", "in_progress", "done"}


TODO_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "todo_write",
        "Create or update an in-memory TODO item for the current task.",
        {
            "action": {
                "type": "string",
                "enum": ["add", "update"],
                "description": "Whether to add a new TODO or update an existing one.",
            },
            "task": {
                "type": "string",
                "description": "The task text when action=add.",
            },
            "priority": {
                "type": "string",
                "description": "Task priority when action=add.",
                "default": "normal",
            },
            "task_id": {
                "type": "string",
                "description": "The TODO id when action=update.",
            },
            "status": {
                "type": "string",
                "enum": sorted(VALID_TODO_STATUSES),
                "description": "New status when action=update.",
            },
        },
        ["action"],
    ),
    _schema(
        "todo_read",
        "List all in-memory TODO items and their current status.",
        {},
        [],
    ),
]

class TodoManager:
    """Manage short-lived in-memory TODO items for the active session."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, str]] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, task: str, priority: str = "normal") -> str:
        with self._lock:
            task_text = task.strip()
            if not task_text:
                raise ValueError("task must not be empty")

            task_id = f"t{self._next_id}"
            self._next_id += 1
            self.tasks[task_id] = {
                "task": task_text,
                "status": "pending",
                "priority": priority.strip() or "normal",
            }
            return f"Added TODO {task_id} [{self.tasks[task_id]['priority']}]: {task_text}"

    def update(self, task_id: str, status: str) -> str:
        with self._lock:
            normalized_id = task_id.strip()
            if normalized_id not in self.tasks:
                raise ValueError(f"Unknown TODO id: {task_id}")

            normalized_status = status.strip()
            if normalized_status not in VALID_TODO_STATUSES:
                valid = ", ".join(sorted(VALID_TODO_STATUSES))
                raise ValueError(f"Invalid TODO status: {status}. Expected one of: {valid}")

            self.tasks[normalized_id]["status"] = normalized_status
            return f"Updated TODO {normalized_id} -> {normalized_status}"

    def list(self) -> str:
        with self._lock:
            if not self.tasks:
                return "No TODO items."

            lines = ["TODO items:"]
            for task_id, item in self.tasks.items():
                lines.append(f"- {task_id} [{item['status']}] ({item['priority']}) {item['task']}")
            return "\n".join(lines)

    def get_nag_reminder(self) -> str | None:
        with self._lock:
            pending_lines = [
                f"- {task_id} [{item['status']}] ({item['priority']}) {item['task']}"
                for task_id, item in self.tasks.items()
                if item["status"] != "done"
            ]
            if not pending_lines:
                return None

            return "\n".join(
                [
                    "You still have unfinished TODO items. Keep them in sync with your progress.",
                    *pending_lines,
                ]
            )


def make_todo_handlers(todo_manager: TodoManager) -> dict[str, Any]:
    def _todo_write(
        action: str,
        task: str | None = None,
        priority: str = "normal",
        task_id: str | None = None,
        status: str | None = None,
    ) -> str:
        if action == "add":
            if task is None:
                raise ValueError("task is required when action=add")
            return todo_manager.add(task=task, priority=priority)

        if action == "update":
            if task_id is None:
                raise ValueError("task_id is required when action=update")
            if status is None:
                raise ValueError("status is required when action=update")
            return todo_manager.update(task_id=task_id, status=status)

        raise ValueError(f"Unknown todo_write action: {action}")

    return {
        "todo_write": _todo_write,
        "todo_read": todo_manager.list,
    }
