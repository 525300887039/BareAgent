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
        self._lock = threading.Lock()

    def notify(self, task_id: str, message: str, *, status: str = "failed") -> None:
        """Post an external notification onto the same channel as task completions.

        Used by subsystems that have nothing to run in a background thread but
        still want their event surfaced through the REPL's background-update
        injection (see ``src/concurrency/notification.py``). MCP disconnect
        events flow through here so the user sees them between LLM turns even
        when no MCP tool was in-flight.
        """
        self._queue.put(
            {
                "task_id": task_id,
                "status": status,
                "error": message,
            }
        )

    def submit(self, task_id: str, fn: Callable[..., Any], *args: Any) -> str:
        with self._lock:
            # Prune dead threads to prevent unbounded growth.
            self._threads = {tid: t for tid, t in self._threads.items() if t.is_alive()}
            active_thread = self._threads.get(task_id)
            if active_thread is not None:
                raise ValueError(f"Background task already running: {task_id}")

            thread = threading.Thread(
                target=self._run,
                args=(task_id, fn, *args),
                daemon=True,
            )
            self._threads[task_id] = thread
            thread.start()
            return task_id

    def is_running(self, task_id: str) -> bool:
        """Return True if a live (not-yet-finished) thread is registered for ``task_id``.

        Read-only liveness probe used by callers that track long-lived background
        work by id (e.g. team teammates registered as ``team:<session>:<name>``).
        Mirrors the ``is_alive()`` checks already used by ``submit`` /
        ``drain_notifications`` without mutating the thread registry.
        """
        with self._lock:
            thread = self._threads.get(task_id)
            return thread is not None and thread.is_alive()

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
                break

        with self._lock:
            dead = [tid for tid, t in self._threads.items() if not t.is_alive()]
            for tid in dead:
                del self._threads[tid]

        return notifications
