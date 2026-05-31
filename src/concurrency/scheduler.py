from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.fileutil import generate_random_id

logger = logging.getLogger(__name__)

# Guard against `/loop 0 ...` hammering the background pool. Intervals below
# this floor are rejected at `add()` time.
MIN_INTERVAL_SEC = 5.0


class SchedulerError(Exception):
    """Raised when a scheduled job cannot be created (bad interval / command)."""


@dataclass(slots=True)
class ScheduledJob:
    """A shell command repeated on a fixed interval until cancelled.

    ``run_count`` is mutated in place by ``Scheduler._fire`` each time the job
    triggers; it is the only field that changes after creation.
    """

    job_id: str
    interval_sec: float
    command: str
    run_count: int = field(default=0)


class Scheduler:
    """Fire shell commands on fixed intervals via repeated ``threading.Timer`` arms.

    The scheduler only owns the *timing*: each fire hands the command to the
    injected ``notifier`` (a ``BackgroundManager``) so execution and result
    surfacing reuse the existing background-notification channel. The scheduler
    never touches ``messages`` / ``console`` itself — that separation is what
    keeps it thread-safe against the blocking REPL main loop.
    """

    def __init__(
        self,
        *,
        runner: Callable[[str], Any],
        notifier: Any,
    ) -> None:
        # ``runner`` receives a command string and runs it (REPL injects
        # ``partial(run_bash, cwd=workspace, raise_on_error=True)``).
        self._runner = runner
        # ``notifier`` is the BackgroundManager; we only use ``submit``/``notify``.
        self._notifier = notifier
        self._lock = threading.Lock()
        self._jobs: dict[str, ScheduledJob] = {}
        self._timers: dict[str, threading.Timer] = {}

    def add(self, interval_sec: float, command: str) -> ScheduledJob:
        if interval_sec < MIN_INTERVAL_SEC:
            raise SchedulerError(
                f"Interval must be at least {MIN_INTERVAL_SEC:g} seconds (got {interval_sec:g})."
            )
        command = command.strip()
        if not command:
            raise SchedulerError("Command must not be empty.")
        job_id = f"loop-{generate_random_id(6)}"
        job = ScheduledJob(job_id=job_id, interval_sec=interval_sec, command=command)
        with self._lock:
            self._jobs[job_id] = job
            self._arm(job)
        return job

    def _arm(self, job: ScheduledJob) -> None:
        # Caller holds ``self._lock``. ``Timer.start`` only spins up a thread,
        # so starting it under the lock is cheap and avoids a race where a
        # concurrent ``cancel`` misses the freshly-armed timer.
        timer = threading.Timer(job.interval_sec, self._fire, args=(job.job_id,))
        timer.daemon = True
        self._timers[job.job_id] = timer
        timer.start()

    def _fire(self, job_id: str) -> None:
        # Runs on the Timer thread: nothing here may raise (a dying daemon
        # thread is a silent debugging trap — see error-handling spec).
        try:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    # Cancelled between Timer fire and lock acquisition.
                    return
                job.run_count += 1
                run_id = f"{job_id}-{job.run_count}"
                command = job.command
                # Re-arm the next fire while still holding the lock so the
                # repeat schedule is self-perpetuating until cancelled.
                self._arm(job)
            # Hand execution to the background pool outside the lock. A unique
            # run_id per fire avoids BackgroundManager's same-task-id dedup
            # ValueError; any submit failure is swallowed (surfaced via notify)
            # so the schedule keeps running.
            try:
                self._notifier.submit(run_id, self._runner, command)
            except Exception as exc:
                try:
                    self._notifier.notify(
                        run_id,
                        f"Failed to dispatch scheduled command: {type(exc).__name__}: {exc}",
                        status="failed",
                    )
                except Exception:
                    logger.exception("Scheduler notify failed for job %s", job_id)
        except Exception:
            logger.exception("Scheduler fire failed for job %s", job_id)

    def list(self) -> list[ScheduledJob]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            timer = self._timers.pop(job_id, None)
            job = self._jobs.pop(job_id, None)
            if timer is not None:
                timer.cancel()
            return job is not None

    def cancel_all(self) -> None:
        # Idempotent: safe to call multiple times (e.g. exit cleanup).
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._jobs.clear()
