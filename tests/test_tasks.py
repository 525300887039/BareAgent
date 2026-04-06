from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.concurrency.background import BackgroundManager
from src.concurrency.notification import inject_notifications
from src.core.tools import get_handlers
from src.planning.tasks import TaskManager


def test_task_manager_persists_created_tasks(tmp_path: Path) -> None:
    task_file = tmp_path / ".tasks.json"
    manager = TaskManager(task_file)

    created = manager.create("写代码", description="实现核心逻辑")

    assert task_file.exists()
    reloaded = TaskManager(task_file)
    assert reloaded.get(created.id).title == "写代码"
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert created.id in payload["tasks"]


def test_task_manager_ready_tasks_follow_dependency_chain(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / ".tasks.json")
    task_a = manager.create("A")
    task_b = manager.create("B", depends_on=[task_a.id])
    task_c = manager.create("C", depends_on=[task_b.id])

    assert [task.id for task in manager.get_ready_tasks()] == [task_a.id]

    manager.update(task_a.id, status="done")
    assert [task.id for task in manager.get_ready_tasks()] == [task_b.id]

    manager.update(task_b.id, status="done")
    assert [task.id for task in manager.get_ready_tasks()] == [task_c.id]


def test_task_manager_rejects_cyclic_dependency_graph_on_load(tmp_path: Path) -> None:
    task_file = tmp_path / ".tasks.json"
    task_file.write_text(
        json.dumps(
            {
                "tasks": {
                    "taskA111": {
                        "id": "taskA111",
                        "title": "A",
                        "description": "",
                        "status": "pending",
                        "depends_on": ["taskB222"],
                        "created_at": "2026-04-03T00:00:00+00:00",
                        "updated_at": "2026-04-03T00:00:00+00:00",
                    },
                    "taskB222": {
                        "id": "taskB222",
                        "title": "B",
                        "description": "",
                        "status": "pending",
                        "depends_on": ["taskA111"],
                        "created_at": "2026-04-03T00:00:00+00:00",
                        "updated_at": "2026-04-03T00:00:00+00:00",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Cyclic task dependency detected"):
        TaskManager(task_file)


def test_background_manager_submit_and_drain(tmp_path: Path) -> None:
    _ = tmp_path
    manager = BackgroundManager()
    manager.submit("job-1", lambda value: value.upper(), "done")

    notifications = _wait_for_notifications(manager)

    assert notifications == [
        {
            "task_id": "job-1",
            "status": "done",
            "result": "DONE",
        }
    ]


def test_inject_notifications_appends_user_message() -> None:
    manager = BackgroundManager()
    manager.submit("job-2", lambda: "finished")
    messages = [
        {"role": "system", "content": "You are BareAgent."},
        {"role": "user", "content": "继续处理当前请求"},
    ]

    deadline = time.time() + 2
    while time.time() < deadline:
        inject_notifications(messages, manager)
        if len(messages) > 2:
            break
        time.sleep(0.01)

    assert messages[-1] == {"role": "user", "content": "继续处理当前请求"}
    assert messages[-2]["role"] == "system"
    assert "后台任务更新" in str(messages[-2]["content"])
    assert "job-2" in str(messages[-2]["content"])


def test_background_run_marks_failed_shell_commands_as_failed(tmp_path: Path) -> None:
    manager = BackgroundManager()
    handlers = get_handlers(tmp_path, bg_manager=manager)
    command = 'Write-Error "boom"; exit 1' if os.name == "nt" else 'echo boom >&2; exit 1'

    submission = handlers["background_run"](command=command, task_id="job-fail")
    notifications = _wait_for_notifications(manager)

    assert submission == "Submitted background task job-fail"
    assert notifications[0]["task_id"] == "job-fail"
    assert notifications[0]["status"] == "failed"
    assert "Command failed with exit code" in str(notifications[0]["error"])


def _wait_for_notifications(manager: BackgroundManager) -> list[dict[str, object]]:
    deadline = time.time() + 2
    while time.time() < deadline:
        notifications = manager.drain_notifications()
        if notifications:
            return notifications
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for background notification")
