from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any


class BackgroundManager:
    """Run slow operations in daemon threads and collect completion notifications."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, task_id: str, fn: Callable[..., Any], *args: Any) -> str:
        active_thread = self._threads.get(task_id)
        if active_thread is not None and active_thread.is_alive():
            raise ValueError(f"Background task already running: {task_id}")

        thread = threading.Thread(
            target=self._run,
            args=(task_id, fn, *args),
            daemon=True,
        )
        self._threads[task_id] = thread
        thread.start()
        return task_id

    def _run(self, task_id: str, fn: Callable[..., Any], *args: Any) -> None:
        try:
            result = fn(*args)
        except Exception as exc:
            self._queue.put(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        self._queue.put(
            {
                "task_id": task_id,
                "status": "done",
                "result": result,
            }
        )

    def drain_notifications(self) -> list[dict[str, Any]]:
        notifications: list[dict[str, Any]] = []
        while True:
            try:
                notifications.append(self._queue.get_nowait())
            except queue.Empty:
                return notifications
