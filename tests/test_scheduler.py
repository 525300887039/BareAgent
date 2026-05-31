from __future__ import annotations

from typing import Any, cast

import pytest

from src.concurrency.scheduler import (
    MIN_INTERVAL_SEC,
    ScheduledJob,
    Scheduler,
    SchedulerError,
)
from src.ui.console import AgentConsole


class _FakeNotifier:
    """Stand-in for BackgroundManager: records submit/notify calls.

    ``submit`` can be told to raise to exercise the scheduler's swallow path.
    """

    def __init__(self, *, submit_raises: BaseException | None = None) -> None:
        self.submitted: list[tuple[str, str]] = []
        self.notified: list[tuple[str, str, str]] = []
        self._submit_raises = submit_raises

    def submit(self, task_id: str, fn: Any, *args: Any) -> str:
        if self._submit_raises is not None:
            raise self._submit_raises
        # Mirror the real signature: fn is the runner, args[0] is the command.
        command = args[0] if args else ""
        self.submitted.append((task_id, command))
        return task_id

    def notify(self, task_id: str, message: str, *, status: str = "failed") -> None:
        self.notified.append((task_id, message, status))


def _make_scheduler(notifier: _FakeNotifier | None = None) -> tuple[Scheduler, _FakeNotifier]:
    notifier = notifier or _FakeNotifier()
    # Runner is irrelevant for the fake notifier (which never calls it); use a
    # no-op so we never touch a real subprocess.
    scheduler = Scheduler(runner=lambda command: None, notifier=notifier)
    return scheduler, notifier


# ---------------------------------------------------------------------------
# add() validation
# ---------------------------------------------------------------------------


def test_add_rejects_interval_below_minimum() -> None:
    scheduler, _ = _make_scheduler()
    with pytest.raises(SchedulerError):
        scheduler.add(MIN_INTERVAL_SEC - 1, "echo hi")
    # No timer should have been armed.
    assert scheduler.list() == []


def test_add_rejects_empty_command() -> None:
    scheduler, _ = _make_scheduler()
    with pytest.raises(SchedulerError):
        scheduler.add(MIN_INTERVAL_SEC, "   ")
    assert scheduler.list() == []


def test_add_creates_and_arms_job() -> None:
    scheduler, _ = _make_scheduler()
    job = scheduler.add(MIN_INTERVAL_SEC, "echo hi")
    try:
        assert isinstance(job, ScheduledJob)
        assert job.command == "echo hi"
        assert job.interval_sec == MIN_INTERVAL_SEC
        assert job.run_count == 0
        assert job.job_id.startswith("loop-")
        assert scheduler.list() == [job]
    finally:
        scheduler.cancel_all()


# ---------------------------------------------------------------------------
# list / cancel / cancel_all
# ---------------------------------------------------------------------------


def test_list_returns_snapshot_copy() -> None:
    scheduler, _ = _make_scheduler()
    scheduler.add(MIN_INTERVAL_SEC, "a")
    scheduler.add(MIN_INTERVAL_SEC, "b")
    try:
        jobs = scheduler.list()
        assert len(jobs) == 2
        # Mutating the returned list must not affect internal state.
        jobs.clear()
        assert len(scheduler.list()) == 2
    finally:
        scheduler.cancel_all()


def test_cancel_existing_job_returns_true_and_removes_it() -> None:
    scheduler, _ = _make_scheduler()
    job = scheduler.add(MIN_INTERVAL_SEC, "echo hi")
    assert scheduler.cancel(job.job_id) is True
    assert scheduler.list() == []
    # The timer must no longer be tracked.
    assert job.job_id not in scheduler._timers


def test_cancel_missing_job_returns_false() -> None:
    scheduler, _ = _make_scheduler()
    assert scheduler.cancel("loop-nope") is False


def test_cancel_all_is_idempotent() -> None:
    scheduler, _ = _make_scheduler()
    scheduler.add(MIN_INTERVAL_SEC, "a")
    scheduler.add(MIN_INTERVAL_SEC, "b")
    scheduler.cancel_all()
    assert scheduler.list() == []
    # Calling again must not raise.
    scheduler.cancel_all()
    assert scheduler.list() == []


# ---------------------------------------------------------------------------
# _fire behavior (deterministic — no wall-clock sleep)
# ---------------------------------------------------------------------------


def test_fire_submits_command_increments_count_and_rearms() -> None:
    scheduler, notifier = _make_scheduler()
    job = scheduler.add(MIN_INTERVAL_SEC, "gh run list")
    try:
        first_timer = scheduler._timers[job.job_id]

        scheduler._fire(job.job_id)

        # Submitted once with a unique run-scoped task id and the command.
        assert len(notifier.submitted) == 1
        task_id, command = notifier.submitted[0]
        assert task_id == f"{job.job_id}-1"
        assert command == "gh run list"
        # run_count incremented.
        assert job.run_count == 1
        # A fresh timer was armed (self-rescheduling repeat).
        assert scheduler._timers[job.job_id] is not first_timer

        # Second fire keeps incrementing and uses a distinct run id.
        scheduler._fire(job.job_id)
        assert job.run_count == 2
        assert notifier.submitted[1][0] == f"{job.job_id}-2"
    finally:
        scheduler.cancel_all()


def test_fire_on_cancelled_job_is_noop() -> None:
    scheduler, notifier = _make_scheduler()
    job = scheduler.add(MIN_INTERVAL_SEC, "echo hi")
    scheduler.cancel(job.job_id)

    # Firing a cancelled job must not submit or re-arm.
    scheduler._fire(job.job_id)
    assert notifier.submitted == []
    assert job.job_id not in scheduler._timers


def test_fire_swallows_submit_failure_and_rearms() -> None:
    notifier = _FakeNotifier(submit_raises=ValueError("already running"))
    scheduler, _ = _make_scheduler(notifier)
    job = scheduler.add(MIN_INTERVAL_SEC, "echo hi")
    try:
        # Must not raise even though submit blows up.
        scheduler._fire(job.job_id)
        assert job.run_count == 1
        # Failure surfaced via notify, and the job re-armed.
        assert len(notifier.notified) == 1
        assert job.job_id in scheduler._timers
    finally:
        scheduler.cancel_all()


# ---------------------------------------------------------------------------
# /loop command parsing (_dispatch_loop_command)
# ---------------------------------------------------------------------------


class _FakeConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)


class _FakeScheduler:
    """Records calls so we can assert how the dispatcher routed each form."""

    def __init__(self, *, add_raises: SchedulerError | None = None) -> None:
        self.added: list[tuple[float, str]] = []
        self.cancelled: list[str] = []
        self.cancel_all_count = 0
        self._jobs: list[ScheduledJob] = []
        self._add_raises = add_raises

    def add(self, interval_sec: float, command: str) -> ScheduledJob:
        if self._add_raises is not None:
            raise self._add_raises
        self.added.append((interval_sec, command))
        job = ScheduledJob(job_id="loop-test01", interval_sec=interval_sec, command=command)
        self._jobs.append(job)
        return job

    def list(self) -> list[ScheduledJob]:
        return list(self._jobs)

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return job_id == "loop-known"

    def cancel_all(self) -> None:
        self.cancel_all_count += 1


def _dispatch(text: str, scheduler: Any) -> _FakeConsole:
    from src.main import _dispatch_loop_command

    console = _FakeConsole()
    _dispatch_loop_command(
        text,
        scheduler=scheduler,
        ui_console=cast(AgentConsole, console),
    )
    return console


def test_dispatch_no_args_shows_usage_when_empty() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop", scheduler)
    assert console.errors == []
    assert any("no scheduled commands" in s for s in console.statuses)


def test_dispatch_list_shows_jobs() -> None:
    scheduler = _FakeScheduler()
    scheduler.add(10, "gh run list")
    console = _dispatch("/loop list", scheduler)
    assert any("gh run list" in s for s in console.statuses)


def test_dispatch_create_passes_interval_and_command() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop 30 gh run list --limit 5", scheduler)
    assert scheduler.added == [(30.0, "gh run list --limit 5")]
    assert console.errors == []
    # Create path warns about no permission confirmation.
    assert any("WITHOUT permission" in s for s in console.statuses)


def test_dispatch_create_non_numeric_interval_errors() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop soon echo hi", scheduler)
    assert scheduler.added == []
    assert console.errors


def test_dispatch_create_missing_command_errors() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop 30", scheduler)
    assert scheduler.added == []
    assert console.errors


def test_dispatch_create_surfaces_scheduler_error() -> None:
    scheduler = _FakeScheduler(add_raises=SchedulerError("too fast"))
    console = _dispatch("/loop 1 echo hi", scheduler)
    assert console.errors == ["too fast"]


def test_dispatch_cancel_known_and_unknown() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop cancel loop-known", scheduler)
    assert scheduler.cancelled == ["loop-known"]
    assert any("Cancelled" in s for s in console.statuses)

    scheduler2 = _FakeScheduler()
    console2 = _dispatch("/loop cancel loop-missing", scheduler2)
    assert console2.errors


def test_dispatch_cancel_without_id_errors() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop cancel", scheduler)
    assert console.errors


def test_dispatch_clear_cancels_all() -> None:
    scheduler = _FakeScheduler()
    console = _dispatch("/loop clear", scheduler)
    assert scheduler.cancel_all_count == 1
    assert any("Cancelled all" in s for s in console.statuses)
