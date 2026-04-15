from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.core.fileutil import atomic_write_json, generate_random_id, utc_timestamp_iso
from src.core.schema import tool_schema as _schema

TASK_STATUSES = {"pending", "in_progress", "done", "failed"}


@dataclass(slots=True)
class Task:
    id: str
    title: str
    description: str
    status: str
    depends_on: list[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TASK_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "task_create",
        "Create a persisted task with optional dependency task IDs.",
        {
            "title": {
                "type": "string",
                "description": "Short task title.",
            },
            "description": {
                "type": "string",
                "description": "Optional task description.",
                "default": "",
            },
            "depends_on": {
                "type": "array",
                "description": "Optional dependency task IDs.",
                "items": {"type": "string"},
                "default": [],
            },
        },
        ["title"],
    ),
    _schema(
        "task_update",
        "Update a persisted task status and/or title.",
        {
            "task_id": {
                "type": "string",
                "description": "Task ID to update.",
            },
            "status": {
                "type": "string",
                "enum": sorted(TASK_STATUSES),
                "description": "Optional new task status.",
            },
            "title": {
                "type": "string",
                "description": "Optional new task title.",
            },
        },
        ["task_id"],
    ),
    _schema(
        "task_get",
        "Get a single persisted task by ID.",
        {
            "task_id": {
                "type": "string",
                "description": "Task ID to look up.",
            }
        },
        ["task_id"],
    ),
    _schema(
        "task_list",
        "List persisted tasks, optionally filtered by status.",
        {
            "status": {
                "type": "string",
                "enum": sorted(TASK_STATUSES),
                "description": "Optional task status filter.",
            }
        },
        [],
    ),
]


class TaskManager:
    """Persist tasks on disk and enforce dependency validation."""

    def __init__(self, task_file: str | Path = ".tasks.json") -> None:
        self.task_file = Path(task_file)
        self.tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self._load()

    def create(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
    ) -> Task:
        with self._lock:
            normalized_title = title.strip()
            if not normalized_title:
                raise ValueError("title must not be empty")

            normalized_depends_on = self._normalize_depends_on(depends_on)
            self._ensure_dependencies_exist(normalized_depends_on)
            now = self._timestamp()
            task = Task(
                id=self._generate_task_id(),
                title=normalized_title,
                description=description.strip(),
                status="pending",
                depends_on=normalized_depends_on,
                created_at=now,
                updated_at=now,
            )
            self.tasks[task.id] = task
            try:
                self._ensure_acyclic()
            except Exception:
                del self.tasks[task.id]
                raise
            self._save()
            return self._copy_task(task)

    def update(
        self,
        task_id: str,
        status: str | None = None,
        title: str | None = None,
        expected_status: str | None = None,
    ) -> Task:
        with self._lock:
            task = self._get_unsafe(task_id)
            if expected_status is not None:
                normalized_expected_status = expected_status.strip()
                self._validate_status(normalized_expected_status)
                if task.status != normalized_expected_status:
                    raise ValueError(
                        f"Task {task.id} status is {task.status}, expected {normalized_expected_status}"
                    )

            changed = False

            if status is not None:
                normalized_status = status.strip()
                self._validate_status(normalized_status)
                if task.status != normalized_status:
                    task.status = normalized_status
                    changed = True

            if title is not None:
                normalized_title = title.strip()
                if not normalized_title:
                    raise ValueError("title must not be empty")
                if task.title != normalized_title:
                    task.title = normalized_title
                    changed = True

            if changed:
                task.updated_at = self._timestamp()
                self._save()

            return self._copy_task(task)

    def get(self, task_id: str) -> Task:
        with self._lock:
            return self._copy_task(self._get_unsafe(task_id))

    def _get_unsafe(self, task_id: str) -> Task:
        """Return the internal task reference. Caller must hold self._lock."""
        normalized_id = task_id.strip()
        task = self.tasks.get(normalized_id)
        if task is None:
            raise ValueError(f"Unknown task id: {task_id}")
        return task

    def list(self, status: str | None = None) -> list[Task]:
        with self._lock:
            if status is None:
                return [self._copy_task(t) for t in self.tasks.values()]

            normalized_status = status.strip()
            self._validate_status(normalized_status)
            return [
                self._copy_task(t)
                for t in self.tasks.values()
                if t.status == normalized_status
            ]

    def get_ready_tasks(self) -> list[Task]:
        with self._lock:
            ready_tasks: list[Task] = []
            for task in self.tasks.values():
                if task.status != "pending":
                    continue
                if all(
                    self.tasks.get(dep_id) is not None
                    and self.tasks[dep_id].status == "done"
                    for dep_id in task.depends_on
                ):
                    ready_tasks.append(self._copy_task(task))
            return ready_tasks

    @staticmethod
    def _copy_task(task: Task) -> Task:
        """Return a shallow copy with an independent depends_on list."""
        copied = copy.copy(task)
        copied.depends_on = list(task.depends_on)
        return copied

    def _save(self) -> None:
        payload = {
            "tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()}
        }
        atomic_write_json(self.task_file, payload)

    def _load(self) -> None:
        try:
            with self.task_file.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except FileNotFoundError:
            self.tasks = {}
            return

        if not isinstance(payload, dict):
            raise ValueError("Task file must contain a JSON object")

        raw_tasks = payload.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            raise ValueError("Task file 'tasks' field must be an object")

        loaded_tasks: dict[str, Task] = {}
        for task_id, raw_task in raw_tasks.items():
            if not isinstance(raw_task, dict):
                raise ValueError(f"Invalid task payload for {task_id}")
            loaded_tasks[task_id] = Task(**raw_task)

        self.tasks = loaded_tasks
        self._validate_graph()

    def _validate_graph(self) -> None:
        for task in self.tasks.values():
            self._validate_status(task.status)
            self._ensure_dependencies_exist(task.depends_on)
        self._ensure_acyclic()

    def _normalize_depends_on(self, depends_on: list[str] | None) -> list[str]:
        if depends_on is None:
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for dependency_id in depends_on:
            normalized_id = str(dependency_id).strip()
            if not normalized_id:
                raise ValueError("depends_on entries must not be empty")
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            normalized.append(normalized_id)
        return normalized

    def _ensure_dependencies_exist(self, depends_on: list[str]) -> None:
        for dependency_id in depends_on:
            if dependency_id not in self.tasks:
                raise ValueError(f"Unknown dependency task id: {dependency_id}")

    def _ensure_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def _dfs(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"Cyclic task dependency detected at: {task_id}")
            if task_id in visited:
                return

            visiting.add(task_id)
            for dependency_id in self.tasks[task_id].depends_on:
                _dfs(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in self.tasks:
            _dfs(task_id)

    def _generate_task_id(self) -> str:
        while True:
            task_id = generate_random_id(8)
            if task_id not in self.tasks:
                return task_id

    def _timestamp(self) -> str:
        return utc_timestamp_iso()

    def _validate_status(self, status: str) -> None:
        if status not in TASK_STATUSES:
            valid = ", ".join(sorted(TASK_STATUSES))
            raise ValueError(f"Invalid task status: {status}. Expected one of: {valid}")


def make_task_handlers(task_manager: TaskManager) -> dict[str, Any]:
    def _task_create(
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        return task_manager.create(
            title=title,
            description=description,
            depends_on=depends_on,
        ).to_dict()

    def _task_update(
        task_id: str,
        status: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if status is None and title is None:
            raise ValueError("status or title is required")
        return task_manager.update(
            task_id=task_id, status=status, title=title
        ).to_dict()

    return {
        "task_create": _task_create,
        "task_update": _task_update,
        "task_get": lambda task_id: task_manager.get(task_id).to_dict(),
        "task_list": lambda status=None: [
            task.to_dict() for task in task_manager.list(status=status)
        ],
    }
