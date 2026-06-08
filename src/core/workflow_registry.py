"""Session-scoped, thread-safe registry of workflow runs.

Backs two features layered on top of the deterministic ``workflow`` tool
(see ``src/core/workflow.py``): the ``/workflows`` panel (list / inspect runs)
and ``resume`` (reuse a prior run's node results). Unlike the pure
``src/core/workflow.py`` engine, this module is *stateful and threaded*: a
background workflow runs in a daemon thread and updates its run here while the
REPL's main thread reads snapshots for the panel, so every method holds a lock.

Lifecycle mirrors :class:`src.planning.subagent_registry.SubagentRegistry` and
``spawned_agents``: in-memory, capped FIFO, cleared by the REPL on ``/new`` /
``/resume`` / ``/import`` / ``/clear`` and kept across ``/compact``. On-disk
persistence (cross-restart resume) is intentionally out of scope.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from enum import Enum

from src.core.fileutil import generate_random_id
from src.core.workflow import NodeResult, NodeStatus, WorkflowSpec

_ID_PREFIX = "wf-"
DEFAULT_MAX_RUNS = 50


class RunStatus(Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class WorkflowRun:
    """One workflow invocation's live (or terminal) state.

    ``results`` is replaced key-by-key as nodes finish (whole ``NodeResult``
    objects swapped in, never mutated in place), so a shallow copy taken under
    the registry lock is a consistent snapshot. ``delivered`` dedups the async
    result feedback: a finished background run is injected into the LLM exactly
    once (see ``main.py:_drain_workflow_results``).
    """

    run_id: str
    spec: WorkflowSpec
    results: dict[str, NodeResult]
    background: bool
    token_budget: int
    status: RunStatus = RunStatus.RUNNING
    tokens_spent: int = 0
    summary: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    delivered: bool = False

    def counts(self) -> dict[str, int]:
        """Tally node statuses for the panel (pending/running shown as 'running')."""
        tally = {"done": 0, "failed": 0, "skipped": 0, "running": 0, "reused": 0}
        for result in self.results.values():
            if result.reused:
                tally["reused"] += 1
            elif result.status is NodeStatus.DONE:
                tally["done"] += 1
            elif result.status is NodeStatus.FAILED:
                tally["failed"] += 1
            elif result.status is NodeStatus.SKIPPED:
                tally["skipped"] += 1
            else:
                tally["running"] += 1
        return tally


class WorkflowRegistry:
    """In-memory, thread-safe, FIFO-capped store of :class:`WorkflowRun`.

    Holds at most ``max_runs`` runs; ``start`` evicts the oldest once over the
    cap (preferring an already-finished run so an in-flight one keeps its panel
    tracking). Updates to a run that has been evicted or cleared are silent
    no-ops, which keeps a lingering background thread from crashing when the
    session has already moved on.
    """

    def __init__(self, max_runs: int = DEFAULT_MAX_RUNS) -> None:
        self._max = max_runs if max_runs > 0 else DEFAULT_MAX_RUNS
        self._lock = threading.Lock()
        self._runs: dict[str, WorkflowRun] = {}

    def generate_id(self) -> str:
        """Return a fresh, unused ``wf-<rand8>`` id."""
        with self._lock:
            while True:
                candidate = _ID_PREFIX + generate_random_id(8)
                if candidate not in self._runs:
                    return candidate

    def start(
        self,
        run_id: str,
        spec: WorkflowSpec,
        *,
        background: bool,
        token_budget: int,
    ) -> WorkflowRun:
        """Register a new RUNNING run (all nodes PENDING) and evict over the cap."""
        run = WorkflowRun(
            run_id=run_id,
            spec=spec,
            results={
                node.id: NodeResult(id=node.id, status=NodeStatus.PENDING) for node in spec.nodes
            },
            background=background,
            token_budget=token_budget,
            started_at=time.time(),
        )
        with self._lock:
            self._runs[run_id] = run
            self._evict_locked()
        return run

    def update_node(self, run_id: str, node_id: str, result: NodeResult) -> None:
        """Replace one node's result (no-op if the run is gone)."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None and node_id in run.results:
                run.results[node_id] = result

    def set_tokens(self, run_id: str, tokens_spent: int) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.tokens_spent = tokens_spent

    def finish(
        self,
        run_id: str,
        *,
        summary: str,
        tokens_spent: int,
        status: RunStatus = RunStatus.DONE,
        error: str = "",
    ) -> None:
        """Mark a run terminal and stage it for one-shot async delivery."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = status
            run.summary = summary
            run.error = error
            run.tokens_spent = tokens_spent
            run.finished_at = time.time()
            run.delivered = False

    def get_for_resume(self, run_id: str) -> tuple[WorkflowSpec, dict[str, NodeResult]] | None:
        """Return ``(spec, results copy)`` of a prior run for resume, or None."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return run.spec, dict(run.results)

    def snapshot(self) -> list[WorkflowRun]:
        """Return consistent copies of every run, newest last (panel list)."""
        with self._lock:
            return [self._copy_locked(run) for run in self._runs.values()]

    def get(self, run_id: str) -> WorkflowRun | None:
        """Return a consistent copy of one run, or None (panel detail)."""
        with self._lock:
            run = self._runs.get(run_id)
            return self._copy_locked(run) if run is not None else None

    def take_undelivered(self) -> list[WorkflowRun]:
        """Return copies of finished, not-yet-delivered runs, marking them delivered.

        Used by the REPL drain to inject a finished background workflow's full
        summary into the LLM exactly once.
        """
        out: list[WorkflowRun] = []
        with self._lock:
            for run in self._runs.values():
                if run.status is not RunStatus.RUNNING and not run.delivered:
                    run.delivered = True
                    out.append(self._copy_locked(run))
        return out

    def clear_finished(self) -> int:
        """Drop every non-RUNNING run; return how many were removed."""
        with self._lock:
            finished = [rid for rid, run in self._runs.items() if run.status is RunStatus.RUNNING]
            removed = len(self._runs) - len(finished)
            self._runs = {rid: self._runs[rid] for rid in finished}
            return removed

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)

    def _evict_locked(self) -> None:
        """Trim to the cap, evicting oldest finished runs first (lock held)."""
        while len(self._runs) > self._max:
            victim = next(
                (rid for rid, run in self._runs.items() if run.status is not RunStatus.RUNNING),
                next(iter(self._runs)),
            )
            del self._runs[victim]

    @staticmethod
    def _copy_locked(run: WorkflowRun) -> WorkflowRun:
        """Snapshot a run with a copied results dict (lock held by caller)."""
        return replace(run, results=dict(run.results))
