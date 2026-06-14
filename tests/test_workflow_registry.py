from __future__ import annotations

from bareagent.core.workflow import NodeResult, NodeStatus, WorkflowNode, WorkflowSpec
from bareagent.core.workflow_registry import (
    DEFAULT_MAX_RUNS,
    RunStatus,
    WorkflowRegistry,
)


def _spec(*ids: str) -> WorkflowSpec:
    return WorkflowSpec(nodes=[WorkflowNode(id=i, prompt=f"p-{i}") for i in ids])


def test_generate_id_is_prefixed_and_unique():
    reg = WorkflowRegistry()
    a = reg.generate_id()
    b = reg.generate_id()
    assert a.startswith("wf-")
    assert a != b


def test_start_creates_running_run_with_pending_nodes():
    reg = WorkflowRegistry()
    spec = _spec("a", "b")
    run = reg.start("wf-1", spec, background=True, token_budget=500)
    assert run.status is RunStatus.RUNNING
    assert run.background is True
    assert run.token_budget == 500
    assert set(run.results) == {"a", "b"}
    assert all(r.status is NodeStatus.PENDING for r in run.results.values())


def test_update_node_and_snapshot_isolation():
    reg = WorkflowRegistry()
    reg.start("wf-1", _spec("a"), background=False, token_budget=0)
    reg.update_node("wf-1", "a", NodeResult(id="a", status=NodeStatus.DONE, output="ok"))
    snap = reg.get("wf-1")
    assert snap is not None
    assert snap.results["a"].status is NodeStatus.DONE
    # Mutating the snapshot must not affect the registry's stored copy.
    snap.results["a"] = NodeResult(id="a", status=NodeStatus.FAILED)
    assert reg.get("wf-1").results["a"].status is NodeStatus.DONE


def test_update_node_missing_run_is_noop():
    reg = WorkflowRegistry()
    # No run started -> silently ignored (lingering bg thread after session clear).
    reg.update_node("wf-ghost", "a", NodeResult(id="a", status=NodeStatus.DONE))
    assert reg.get("wf-ghost") is None


def test_finish_marks_terminal_and_stages_for_delivery():
    reg = WorkflowRegistry()
    reg.start("wf-1", _spec("a"), background=True, token_budget=0)
    reg.finish("wf-1", summary="all good", tokens_spent=42)
    run = reg.get("wf-1")
    assert run.status is RunStatus.DONE
    assert run.summary == "all good"
    assert run.tokens_spent == 42
    assert run.finished_at is not None


def test_take_undelivered_returns_finished_once():
    reg = WorkflowRegistry()
    reg.start("wf-1", _spec("a"), background=True, token_budget=0)
    reg.start("wf-2", _spec("b"), background=True, token_budget=0)
    reg.finish("wf-1", summary="s1", tokens_spent=1)
    first = reg.take_undelivered()
    assert {r.run_id for r in first} == {"wf-1"}  # wf-2 still running
    # Second drain: nothing new (wf-1 already delivered, wf-2 still running).
    assert reg.take_undelivered() == []
    reg.finish("wf-2", summary="s2", tokens_spent=2)
    second = reg.take_undelivered()
    assert {r.run_id for r in second} == {"wf-2"}


def test_get_for_resume_returns_spec_and_results_copy():
    reg = WorkflowRegistry()
    spec = _spec("a")
    reg.start("wf-1", spec, background=False, token_budget=0)
    reg.update_node("wf-1", "a", NodeResult(id="a", status=NodeStatus.DONE, output="o"))
    prior = reg.get_for_resume("wf-1")
    assert prior is not None
    prior_spec, prior_results = prior
    assert prior_spec is spec
    assert prior_results["a"].output == "o"
    assert reg.get_for_resume("wf-missing") is None


def test_clear_finished_keeps_running():
    reg = WorkflowRegistry()
    reg.start("wf-run", _spec("a"), background=True, token_budget=0)
    reg.start("wf-done", _spec("b"), background=True, token_budget=0)
    reg.finish("wf-done", summary="s", tokens_spent=0)
    removed = reg.clear_finished()
    assert removed == 1
    assert reg.get("wf-done") is None
    assert reg.get("wf-run") is not None


def test_fifo_evicts_oldest_finished_first():
    reg = WorkflowRegistry(max_runs=2)
    reg.start("wf-1", _spec("a"), background=True, token_budget=0)
    reg.finish("wf-1", summary="s", tokens_spent=0)  # finished -> preferred victim
    reg.start("wf-2", _spec("b"), background=True, token_budget=0)  # running
    reg.start("wf-3", _spec("c"), background=True, token_budget=0)  # over cap -> evict wf-1
    assert reg.get("wf-1") is None
    assert reg.get("wf-2") is not None
    assert reg.get("wf-3") is not None


def test_clear_removes_all():
    reg = WorkflowRegistry()
    reg.start("wf-1", _spec("a"), background=True, token_budget=0)
    reg.clear()
    assert len(reg) == 0


def test_counts_tallies_statuses():
    reg = WorkflowRegistry()
    reg.start("wf-1", _spec("a", "b", "c", "d"), background=False, token_budget=0)
    reg.update_node("wf-1", "a", NodeResult(id="a", status=NodeStatus.DONE))
    reg.update_node("wf-1", "b", NodeResult(id="b", status=NodeStatus.DONE, reused=True))
    reg.update_node("wf-1", "c", NodeResult(id="c", status=NodeStatus.FAILED))
    # d stays PENDING -> counted as "running"
    counts = reg.get("wf-1").counts()
    assert counts == {"done": 1, "failed": 1, "skipped": 0, "running": 1, "reused": 1}


def test_default_max_runs_constant_used_on_bad_value():
    reg = WorkflowRegistry(max_runs=0)
    assert reg._max == DEFAULT_MAX_RUNS
