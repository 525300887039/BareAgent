from __future__ import annotations

import pytest

from bareagent.core.handlers.workflow import WORKFLOW_TOOL_SCHEMA, run_workflow_tool
from bareagent.core.workflow import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_NODES,
    NodeResult,
    NodeStatus,
    WorkflowError,
    WorkflowNode,
    WorkflowSpec,
    build_node_prompt,
    compute_ready,
    compute_resume_plan,
    format_summary,
    parse_workflow,
    propagate_skips,
    run_workflow,
    validate_workflow,
)

# --- test helpers ---------------------------------------------------------


def _spec(*nodes: WorkflowNode) -> WorkflowSpec:
    return WorkflowSpec(nodes=list(nodes))


def _pending(spec: WorkflowSpec) -> dict[str, NodeResult]:
    return {n.id: NodeResult(id=n.id, status=NodeStatus.PENDING) for n in spec.nodes}


def _sync_map(thunks):
    """Sequential stand-in for the thread-pool map (deterministic in tests)."""
    return [thunk() for thunk in thunks]


# --- parse_workflow -------------------------------------------------------


def test_parse_workflow_basic():
    spec = parse_workflow({"nodes": [{"id": "a", "prompt": "do a"}]})
    assert len(spec.nodes) == 1
    assert spec.nodes[0].id == "a"
    assert spec.nodes[0].prompt == "do a"
    assert spec.nodes[0].depends_on == []
    assert spec.nodes[0].agent_type is None


def test_parse_workflow_optional_fields_and_depends_list():
    spec = parse_workflow(
        {
            "nodes": [
                {
                    "id": "b",
                    "prompt": "p",
                    "agent_type": " explore ",
                    "depends_on": ["a", " ", "c"],
                    "phase": "review",
                    "label": "B node",
                }
            ]
        }
    )
    node = spec.nodes[0]
    assert node.agent_type == "explore"
    assert node.depends_on == ["a", "c"]  # blank entry dropped, stripped
    assert node.phase == "review"
    assert node.label == "B node"


def test_parse_workflow_depends_on_string_coerced_to_list():
    spec = parse_workflow({"nodes": [{"id": "b", "prompt": "p", "depends_on": "a"}]})
    assert spec.nodes[0].depends_on == ["a"]


def test_parse_workflow_non_dict_node_becomes_blank_id():
    spec = parse_workflow({"nodes": ["not a dict"]})
    assert spec.nodes[0].id == ""


def test_parse_workflow_non_dict_input_raises():
    with pytest.raises(WorkflowError):
        parse_workflow(None)


def test_parse_workflow_nodes_not_list_raises():
    with pytest.raises(WorkflowError):
        parse_workflow({"nodes": "oops"})


# --- validate_workflow ----------------------------------------------------


def test_validate_ok():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
    )
    assert validate_workflow(spec) == []


def test_validate_empty_workflow():
    errors = validate_workflow(_spec())
    assert any("at least one node" in e for e in errors)


def test_validate_max_nodes_exceeded():
    nodes = [WorkflowNode(id=f"n{i}", prompt="p") for i in range(5)]
    errors = validate_workflow(_spec(*nodes), max_nodes=3)
    assert any("exceeding the limit" in e for e in errors)


def test_validate_blank_id():
    errors = validate_workflow(_spec(WorkflowNode(id="", prompt="p")))
    assert any("non-empty 'id'" in e for e in errors)


def test_validate_duplicate_ids():
    spec = _spec(WorkflowNode(id="a", prompt="p"), WorkflowNode(id="a", prompt="q"))
    errors = validate_workflow(spec)
    assert any("duplicate node id" in e for e in errors)


def test_validate_empty_prompt():
    errors = validate_workflow(_spec(WorkflowNode(id="a", prompt="   ")))
    assert any("empty 'prompt'" in e for e in errors)


def test_validate_self_dependency():
    errors = validate_workflow(_spec(WorkflowNode(id="a", prompt="p", depends_on=["a"])))
    assert any("cannot depend on itself" in e for e in errors)


def test_validate_unknown_dependency():
    errors = validate_workflow(_spec(WorkflowNode(id="a", prompt="p", depends_on=["ghost"])))
    assert any("unknown node 'ghost'" in e for e in errors)


def test_validate_cycle_detected():
    spec = _spec(
        WorkflowNode(id="a", prompt="p", depends_on=["b"]),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
    )
    errors = validate_workflow(spec)
    assert any("cycle" in e for e in errors)


def test_validate_long_cycle_detected():
    spec = _spec(
        WorkflowNode(id="a", prompt="p", depends_on=["c"]),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
        WorkflowNode(id="c", prompt="p", depends_on=["b"]),
    )
    errors = validate_workflow(spec)
    assert any("cycle" in e for e in errors)


def test_validate_diamond_is_acyclic():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
        WorkflowNode(id="c", prompt="p", depends_on=["a"]),
        WorkflowNode(id="d", prompt="p", depends_on=["b", "c"]),
    )
    assert validate_workflow(spec) == []


# --- compute_ready / propagate_skips --------------------------------------


def test_compute_ready_initial_only_rootless():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
    )
    results = _pending(spec)
    ready = compute_ready(spec, results)
    assert [n.id for n in ready] == ["a"]


def test_compute_ready_unblocks_after_dependency_done():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
    )
    results = _pending(spec)
    results["a"] = NodeResult(id="a", status=NodeStatus.DONE, output="x")
    ready = compute_ready(spec, results)
    assert [n.id for n in ready] == ["b"]


def test_propagate_skips_transitive():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
        WorkflowNode(id="c", prompt="p", depends_on=["b"]),
        WorkflowNode(id="d", prompt="p"),  # independent
    )
    results = _pending(spec)
    results["a"] = NodeResult(id="a", status=NodeStatus.FAILED, error="boom")
    skipped = propagate_skips(spec, results)
    assert skipped == {"b", "c"}
    assert results["b"].status is NodeStatus.SKIPPED
    assert results["c"].status is NodeStatus.SKIPPED
    assert results["d"].status is NodeStatus.PENDING  # independent untouched


# --- build_node_prompt ----------------------------------------------------


def test_build_node_prompt_placeholder_substitution():
    node = WorkflowNode(id="b", prompt="Use this: {{a}} -- end", depends_on=["a"])
    upstream = {"a": NodeResult(id="a", status=NodeStatus.DONE, output="RESULT_A")}
    prompt = build_node_prompt(node, upstream)
    assert "Use this: RESULT_A -- end" in prompt
    # Referenced via placeholder, so NOT also auto-appended.
    assert "# Upstream results" not in prompt


def test_build_node_prompt_auto_appends_unreferenced_deps():
    node = WorkflowNode(id="b", prompt="no placeholder", depends_on=["a"])
    upstream = {"a": NodeResult(id="a", status=NodeStatus.DONE, output="RESULT_A")}
    prompt = build_node_prompt(node, upstream)
    assert "# Upstream results" in prompt
    assert "RESULT_A" in prompt
    assert 'from="a"' in prompt


def test_build_node_prompt_unknown_placeholder_left_intact():
    node = WorkflowNode(id="b", prompt="{{ghost}} stays", depends_on=[])
    prompt = build_node_prompt(node, {})
    assert "{{ghost}}" in prompt


# --- format_summary -------------------------------------------------------


def test_format_summary_counts_and_sections():
    spec = _spec(
        WorkflowNode(id="a", prompt="p", label="First"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
        WorkflowNode(id="c", prompt="p", depends_on=["a"], phase="review"),
    )
    results = {
        "a": NodeResult(id="a", status=NodeStatus.DONE, output="done-a"),
        "b": NodeResult(id="b", status=NodeStatus.FAILED, error="kaboom"),
        "c": NodeResult(id="c", status=NodeStatus.SKIPPED, error="upstream"),
    }
    summary = format_summary(spec, results)
    assert "1 done, 1 failed, 1 skipped (of 3 nodes)" in summary
    assert "## [done] a - First" in summary
    assert "done-a" in summary
    assert "## [failed] b" in summary
    assert "Error: kaboom" in summary
    assert "## [skipped] c (phase: review)" in summary


# --- run_workflow ---------------------------------------------------------


def test_run_workflow_linear_threads_results():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
    )

    def execute(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        if node.id == "a":
            return "OUT_A"
        # b sees a's output threaded in
        return "B saw " + upstream["a"].output

    results = run_workflow(spec, execute_node=execute, map_concurrent=_sync_map)
    assert results["a"].status is NodeStatus.DONE
    assert results["b"].status is NodeStatus.DONE
    assert results["b"].output == "B saw OUT_A"


def test_run_workflow_parallel_batch_grouped():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p"),
        WorkflowNode(id="c", prompt="p", depends_on=["a", "b"]),
    )
    batches: list[int] = []

    def recording_map(thunks):
        batches.append(len(thunks))
        return [thunk() for thunk in thunks]

    results = run_workflow(
        spec,
        execute_node=lambda node, up: node.id,
        map_concurrent=recording_map,
    )
    # First batch runs {a, b} together (2), second runs {c} (1).
    assert batches == [2, 1]
    assert all(results[i].status is NodeStatus.DONE for i in ("a", "b", "c"))


def test_run_workflow_failure_skips_downstream_independent_continues():
    spec = _spec(
        WorkflowNode(id="a", prompt="p"),
        WorkflowNode(id="b", prompt="p", depends_on=["a"]),
        WorkflowNode(id="c", prompt="p"),  # independent of a
    )

    def execute(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        if node.id == "a":
            raise ValueError("a failed")
        return "ok-" + node.id

    results = run_workflow(spec, execute_node=execute, map_concurrent=_sync_map)
    assert results["a"].status is NodeStatus.FAILED
    assert "ValueError" in results["a"].error
    assert results["b"].status is NodeStatus.SKIPPED
    assert results["c"].status is NodeStatus.DONE  # independent branch ran


def test_run_workflow_progress_callback_invoked():
    spec = _spec(WorkflowNode(id="a", prompt="p"))
    progress: list[str] = []
    run_workflow(
        spec,
        execute_node=lambda node, up: "x",
        map_concurrent=_sync_map,
        on_progress=progress.append,
    )
    assert any("a" in line for line in progress)


# --- run_workflow_tool (handler shim) -------------------------------------


def test_run_workflow_tool_invalid_returns_error_string():
    out = run_workflow_tool(
        nodes=None,
        execute_node=lambda node, up: "x",
        map_concurrent=_sync_map,
    )
    assert out.startswith("Error:")


def test_run_workflow_tool_validation_error_string():
    out = run_workflow_tool(
        nodes=[{"id": "a", "prompt": "p", "depends_on": ["ghost"]}],
        execute_node=lambda node, up: "x",
        map_concurrent=_sync_map,
    )
    assert "invalid workflow" in out
    assert "ghost" in out


def test_run_workflow_tool_max_nodes_enforced():
    nodes = [{"id": f"n{i}", "prompt": "p"} for i in range(4)]
    out = run_workflow_tool(
        nodes=nodes,
        execute_node=lambda node, up: "x",
        map_concurrent=_sync_map,
        max_nodes=2,
    )
    assert "exceeding the limit" in out


def test_run_workflow_tool_success_returns_summary():
    out = run_workflow_tool(
        nodes=[
            {"id": "a", "prompt": "pa"},
            {"id": "b", "prompt": "pb", "depends_on": ["a"]},
        ],
        execute_node=lambda node, up: "out-" + node.id,
        map_concurrent=_sync_map,
    )
    assert "2 done, 0 failed, 0 skipped" in out
    assert "## [done] a" in out
    assert "## [done] b" in out


def test_workflow_tool_schema_shape():
    assert WORKFLOW_TOOL_SCHEMA["name"] == "workflow"
    params = WORKFLOW_TOOL_SCHEMA["parameters"]
    assert params["required"] == ["nodes"]
    items = params["properties"]["nodes"]["items"]
    assert set(items["required"]) == {"id", "prompt"}


# --- isolation invariants -------------------------------------------------


def test_workflow_not_in_global_tool_set():
    from bareagent.core.tools import get_tools

    names = {tool["name"] for tool in get_tools()}
    assert "workflow" not in names  # main-loop-only; never offered globally


def test_workflow_stripped_for_every_subagent_type():
    from bareagent.planning.agent_types import BUILTIN_AGENT_TYPES, filter_tools

    fake_loop_tools = [WORKFLOW_TOOL_SCHEMA, {"name": "read_file"}]
    for agent_type in BUILTIN_AGENT_TYPES.values():
        kept = {tool["name"] for tool in filter_tools(fake_loop_tools, agent_type)}
        assert "workflow" not in kept, agent_type.name


# --- main.py wiring: config + thread-pool batch ---------------------------


def test_parse_workflow_config_defaults():
    from bareagent.main import WorkflowConfig, _parse_workflow_config

    cfg = _parse_workflow_config({})
    assert cfg == WorkflowConfig()
    assert cfg.enabled is True
    assert cfg.max_concurrency == DEFAULT_MAX_CONCURRENCY
    assert cfg.max_nodes == DEFAULT_MAX_NODES


def test_parse_workflow_config_values():
    from bareagent.main import _parse_workflow_config

    cfg = _parse_workflow_config({"enabled": False, "max_concurrency": 3, "max_nodes": 50})
    assert cfg.enabled is False
    assert cfg.max_concurrency == 3
    assert cfg.max_nodes == 50


def test_parse_workflow_config_bad_values_fall_back():
    from bareagent.main import _parse_workflow_config

    assert _parse_workflow_config({"max_concurrency": "nope"}).max_concurrency == (
        DEFAULT_MAX_CONCURRENCY
    )
    assert _parse_workflow_config({"max_concurrency": 0}).max_concurrency == DEFAULT_MAX_CONCURRENCY
    assert _parse_workflow_config({"max_nodes": -1}).max_nodes == DEFAULT_MAX_NODES


def test_parse_workflow_config_env_override(monkeypatch):
    from bareagent.main import _parse_workflow_config

    monkeypatch.setenv("BAREAGENT_WORKFLOW_ENABLED", "false")
    assert _parse_workflow_config({"enabled": True}).enabled is False


def test_run_node_batch_preserves_order():
    from bareagent.main import _run_node_batch

    def make(value: int):
        return lambda: NodeResult(id=str(value), status=NodeStatus.DONE, output=str(value))

    thunks = [make(i) for i in range(5)]
    results = _run_node_batch(thunks, max_concurrency=4)
    assert [r.id for r in results] == ["0", "1", "2", "3", "4"]


def test_run_node_batch_empty_and_single():
    from bareagent.main import _run_node_batch

    assert _run_node_batch([], max_concurrency=4) == []
    single = _run_node_batch(
        [lambda: NodeResult(id="x", status=NodeStatus.DONE)], max_concurrency=4
    )
    assert [r.id for r in single] == ["x"]


def test_install_workflow_handler_respects_enabled_flag():
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _install_workflow_handler

    # None deps are safe: they are only dereferenced when a node actually runs,
    # which this install-time test never triggers.
    common = dict(
        provider=None,
        base_tools=[],
        permission=None,
        bg_manager=None,
        console=None,
        retry_policy=None,
        max_depth=3,
        default_agent_type="general-purpose",
        max_concurrency=4,
        max_nodes=20,
        registry=WorkflowRegistry(),
        default_token_budget=0,
    )
    disabled: dict = {}
    _install_workflow_handler(disabled, enabled=False, **common)
    assert "workflow" not in disabled  # short-circuit: tool never installed

    enabled: dict = {}
    _install_workflow_handler(enabled, enabled=True, **common)
    assert callable(enabled.get("workflow"))


# --- compute_resume_plan (resume cache) -----------------------------------


def _done(node_id: str, output: str = "") -> NodeResult:
    return NodeResult(id=node_id, status=NodeStatus.DONE, output=output or f"out-{node_id}")


def test_compute_resume_plan_reuses_unchanged_done_node():
    prior = _spec(WorkflowNode(id="a", prompt="pa"))
    new = _spec(WorkflowNode(id="a", prompt="pa"))
    reuse = compute_resume_plan(new, prior, {"a": _done("a")})
    assert set(reuse) == {"a"}
    assert reuse["a"].reused is True
    assert reuse["a"].status is NodeStatus.DONE
    assert reuse["a"].output == "out-a"


def test_compute_resume_plan_skips_changed_prompt():
    prior = _spec(WorkflowNode(id="a", prompt="old"))
    new = _spec(WorkflowNode(id="a", prompt="new"))
    reuse = compute_resume_plan(new, prior, {"a": _done("a")})
    assert reuse == {}


def test_compute_resume_plan_skips_non_done_and_new_nodes():
    prior = _spec(WorkflowNode(id="a", prompt="pa"))
    new = _spec(WorkflowNode(id="a", prompt="pa"), WorkflowNode(id="b", prompt="pb"))
    prior_results = {"a": NodeResult(id="a", status=NodeStatus.FAILED, error="boom")}
    reuse = compute_resume_plan(new, prior, prior_results)
    assert reuse == {}  # a was FAILED, b is new -> nothing reusable


def test_compute_resume_plan_cascade_invalidates_downstream():
    # a changed prompt -> a reruns; b (unchanged) must also rerun because its
    # upstream output changed. c depends on b -> also reruns. d is independent.
    prior = _spec(
        WorkflowNode(id="a", prompt="old"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
        WorkflowNode(id="c", prompt="pc", depends_on=["b"]),
        WorkflowNode(id="d", prompt="pd"),
    )
    new = _spec(
        WorkflowNode(id="a", prompt="new"),  # changed
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),  # unchanged
        WorkflowNode(id="c", prompt="pc", depends_on=["b"]),  # unchanged
        WorkflowNode(id="d", prompt="pd"),  # unchanged, independent
    )
    prior_results = {k: _done(k) for k in ("a", "b", "c", "d")}
    reuse = compute_resume_plan(new, prior, prior_results)
    assert set(reuse) == {"d"}  # only the independent branch survives


# --- run_workflow: reused_results seeding ---------------------------------


def test_run_workflow_seeds_reused_and_skips_execution():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
    )
    executed: list[str] = []

    def execute(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        executed.append(node.id)
        # b should see a's reused output threaded in.
        if node.id == "b":
            assert upstream["a"].output == "cached-a"
        return "fresh-" + node.id

    reused = {"a": NodeResult(id="a", status=NodeStatus.DONE, output="cached-a", reused=True)}
    results = run_workflow(
        spec,
        execute_node=execute,
        map_concurrent=_sync_map,
        reused_results=reused,
    )
    assert executed == ["b"]  # a was reused, not executed
    assert results["a"].reused is True
    assert results["a"].output == "cached-a"
    assert results["b"].status is NodeStatus.DONE


def test_run_workflow_on_node_status_fires_for_all_transitions():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
        WorkflowNode(id="c", prompt="pc", depends_on=["a"]),
    )
    seen: list[tuple[str, NodeStatus, bool]] = []

    def execute(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        if node.id == "a":
            raise ValueError("a failed")
        return "x"

    run_workflow(
        spec,
        execute_node=execute,
        map_concurrent=_sync_map,
        on_node_status=lambda nid, r: seen.append((nid, r.status, r.reused)),
    )
    by_id = {nid: (status, reused) for nid, status, reused in seen}
    assert by_id["a"] == (NodeStatus.FAILED, False)
    assert by_id["b"] == (NodeStatus.SKIPPED, False)
    assert by_id["c"] == (NodeStatus.SKIPPED, False)


# --- run_workflow: token budget -------------------------------------------


def test_run_workflow_budget_exhausted_skips_remaining_layers():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
    )
    executed: list[str] = []
    # Spend jumps to 100 after the first node runs; budget is 50.
    spent = {"value": 0}

    def execute(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        executed.append(node.id)
        spent["value"] = 100
        return "x"

    results = run_workflow(
        spec,
        execute_node=execute,
        map_concurrent=_sync_map,
        token_budget=50,
        tokens_spent=lambda: spent["value"],
    )
    assert executed == ["a"]  # layer with b never launched
    assert results["a"].status is NodeStatus.DONE
    assert results["b"].status is NodeStatus.SKIPPED
    assert "token budget exhausted" in results["b"].error


def test_run_workflow_budget_zero_means_unlimited():
    spec = _spec(WorkflowNode(id="a", prompt="pa"), WorkflowNode(id="b", prompt="pb"))
    results = run_workflow(
        spec,
        execute_node=lambda node, up: "x",
        map_concurrent=_sync_map,
        token_budget=0,
        tokens_spent=lambda: 10_000_000,
    )
    assert all(results[i].status is NodeStatus.DONE for i in ("a", "b"))


# --- format_summary: resume + budget annotations --------------------------


def test_format_summary_annotates_reused_and_budget():
    spec = _spec(
        WorkflowNode(id="a", prompt="pa"),
        WorkflowNode(id="b", prompt="pb", depends_on=["a"]),
    )
    results = {
        "a": NodeResult(id="a", status=NodeStatus.DONE, output="o", reused=True),
        "b": NodeResult(
            id="b", status=NodeStatus.SKIPPED, error="token budget exhausted (60 >= 50 tokens)"
        ),
    }
    out = format_summary(spec, results)
    assert "1 reused from a prior run" in out
    assert "Stopped early: token budget exhausted" in out
    assert "## [reused] a" in out


# --- handler wiring: background / resume / budget --------------------------


class _FakeConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)


class _SyncBgManager:
    """Runs submitted work synchronously so background paths are deterministic."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, task_id, fn, *args):
        self.submitted.append(task_id)
        fn(*args)
        return task_id


def _install_handler(registry, *, default_token_budget=0):
    """Install a workflow handler whose nodes are faked (no real subagent)."""
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _install_workflow_handler

    registry = registry or WorkflowRegistry()
    console = _FakeConsole()
    handlers: dict = {}
    _install_workflow_handler(
        handlers,
        enabled=True,
        provider=None,
        base_tools=[],
        permission=None,
        bg_manager=_SyncBgManager(),
        console=console,
        retry_policy=None,
        max_depth=3,
        default_agent_type="general-purpose",
        max_concurrency=4,
        max_nodes=20,
        registry=registry,
        default_token_budget=default_token_budget,
    )
    return handlers["workflow"], registry, console


def _patch_run_subagent(monkeypatch, calls, *, tokens_per_node=0):
    def fake(**kwargs):
        calls.append(kwargs.get("task", ""))
        tracker = kwargs.get("token_tracker")
        if tracker is not None and tokens_per_node:
            tracker.total_output += tokens_per_node
        return "out-" + kwargs.get("task", "")[:6]

    monkeypatch.setattr("bareagent.main.run_subagent", fake)


def test_handler_sync_returns_summary_and_registers_run(monkeypatch):
    from bareagent.core.workflow_registry import RunStatus, WorkflowRegistry

    calls: list[str] = []
    _patch_run_subagent(monkeypatch, calls)
    handler, registry, _ = _install_handler(WorkflowRegistry())

    out = handler(nodes=[{"id": "a", "prompt": "pa"}, {"id": "b", "prompt": "pb"}])
    assert "2 done" in out
    assert "[workflow run id: wf-" in out
    assert len(calls) == 2
    runs = registry.snapshot()
    assert len(runs) == 1
    assert runs[0].status is RunStatus.DONE


def test_handler_invalid_dag_returns_error_and_registers_nothing(monkeypatch):
    from bareagent.core.workflow_registry import WorkflowRegistry

    _patch_run_subagent(monkeypatch, [])
    handler, registry, _ = _install_handler(WorkflowRegistry())
    out = handler(nodes=None)
    assert out.startswith("Error:")
    assert registry.snapshot() == []  # nothing started for a malformed DAG


def test_handler_background_returns_run_id_and_delivers_via_registry(monkeypatch):
    from bareagent.core.workflow_registry import RunStatus, WorkflowRegistry

    calls: list[str] = []
    _patch_run_subagent(monkeypatch, calls)
    handler, registry, _ = _install_handler(WorkflowRegistry())

    out = handler(nodes=[{"id": "a", "prompt": "pa"}], run_in_background=True)
    assert out.startswith("Workflow wf-")
    assert "started in the background" in out
    # _SyncBgManager ran it inline, so it is already finished + undelivered.
    run = registry.snapshot()[0]
    assert run.status is RunStatus.DONE
    assert run.background is True
    undelivered = registry.take_undelivered()
    assert len(undelivered) == 1
    assert "1 done" in undelivered[0].summary


def test_handler_resume_reuses_unchanged_nodes(monkeypatch):
    from bareagent.core.workflow_registry import WorkflowRegistry

    calls: list[str] = []
    _patch_run_subagent(monkeypatch, calls)
    registry = WorkflowRegistry()
    handler, registry, _ = _install_handler(registry)

    nodes = [{"id": "a", "prompt": "pa"}, {"id": "b", "prompt": "pb", "depends_on": ["a"]}]
    handler(nodes=nodes)
    run_id = registry.snapshot()[0].run_id
    assert len(calls) == 2

    calls.clear()
    # Re-run with one changed node (b); a is unchanged -> reused, b re-runs.
    out = handler(
        nodes=[{"id": "a", "prompt": "pa"}, {"id": "b", "prompt": "CHANGED", "depends_on": ["a"]}],
        resume_from=run_id,
    )
    assert "1 reused from a prior run" in out
    assert len(calls) == 1  # only b re-executed


def test_handler_resume_unknown_id_runs_fresh(monkeypatch):
    from bareagent.core.workflow_registry import WorkflowRegistry

    calls: list[str] = []
    _patch_run_subagent(monkeypatch, calls)
    handler, _, _ = _install_handler(WorkflowRegistry())
    out = handler(nodes=[{"id": "a", "prompt": "pa"}], resume_from="wf-does-not-exist")
    assert "1 done" in out
    assert "reused" not in out
    assert len(calls) == 1


def test_handler_token_budget_skips_remaining(monkeypatch):
    from bareagent.core.workflow_registry import WorkflowRegistry

    calls: list[str] = []
    _patch_run_subagent(monkeypatch, calls, tokens_per_node=100)
    handler, _, _ = _install_handler(WorkflowRegistry())
    out = handler(
        nodes=[{"id": "a", "prompt": "pa"}, {"id": "b", "prompt": "pb", "depends_on": ["a"]}],
        token_budget=50,
    )
    assert "Stopped early: token budget exhausted" in out
    assert len(calls) == 1  # b skipped before its layer launched


# --- panel formatters + dispatch ------------------------------------------


def test_humanize_age_formats():
    from bareagent.main import _humanize_age

    assert _humanize_age(5) == "5s"
    assert _humanize_age(90) == "1m30s"
    assert _humanize_age(120) == "2m"
    assert _humanize_age(3700) == "1h1m"


def test_format_workflow_run_line_and_detail():
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _format_workflow_run_detail, _format_workflow_run_line

    reg = WorkflowRegistry()
    spec = WorkflowSpec(nodes=[WorkflowNode(id="a", prompt="pa", phase="build")])
    reg.start("wf-1", spec, background=True, token_budget=500)
    reg.update_node("wf-1", "a", NodeResult(id="a", status=NodeStatus.DONE, output="hello"))
    snap = reg.get("wf-1")
    line = _format_workflow_run_line(snap, now=snap.started_at + 5)
    assert "wf-1" in line and "[bg]" in line and "/500 tok" in line and "5s ago" in line
    detail = _format_workflow_run_detail(snap, now=snap.started_at + 5)
    assert "[done] a (phase: build)" in detail
    assert "hello" in detail


def test_dispatch_workflows_command_list_detail_clear():
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _dispatch_workflows_command

    reg = WorkflowRegistry()
    spec = WorkflowSpec(nodes=[WorkflowNode(id="a", prompt="pa")])
    reg.start("wf-1", spec, background=False, token_budget=0)
    reg.update_node("wf-1", "a", NodeResult(id="a", status=NodeStatus.DONE, output="o"))
    reg.finish("wf-1", summary="done", tokens_spent=0)

    console = _FakeConsole()
    _dispatch_workflows_command("/workflows", registry=reg, ui_console=console)
    assert any("wf-1" in s for s in console.statuses)

    console = _FakeConsole()
    _dispatch_workflows_command("/workflows wf-1", registry=reg, ui_console=console)
    assert any("[done] a" in s for s in console.statuses)

    console = _FakeConsole()
    _dispatch_workflows_command("/workflows nope", registry=reg, ui_console=console)
    assert console.errors and "No workflow run found" in console.errors[0]

    console = _FakeConsole()
    _dispatch_workflows_command("/workflows clear", registry=reg, ui_console=console)
    assert any("Cleared 1" in s for s in console.statuses)
    assert reg.snapshot() == []


def test_drain_workflow_results_injects_once():
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _drain_workflow_results

    reg = WorkflowRegistry()
    spec = WorkflowSpec(nodes=[WorkflowNode(id="a", prompt="p")])
    reg.start("wf-1", spec, background=True, token_budget=0)
    reg.finish("wf-1", summary="the full summary", tokens_spent=0)

    console = _FakeConsole()
    sink: list[str] = []
    _drain_workflow_results(console, registry=reg, sink=sink)
    assert len(sink) == 1
    assert "the full summary" in sink[0]
    assert 'run="wf-1"' in sink[0]
    # Second drain: already delivered -> nothing added.
    sink.clear()
    _drain_workflow_results(console, registry=reg, sink=sink)
    assert sink == []


def test_drain_skips_sync_runs():
    from bareagent.core.workflow_registry import WorkflowRegistry
    from bareagent.main import _drain_workflow_results

    reg = WorkflowRegistry()
    spec = WorkflowSpec(nodes=[WorkflowNode(id="a", prompt="p")])
    reg.start("wf-1", spec, background=False, token_budget=0)
    reg.finish("wf-1", summary="s", tokens_spent=0)
    sink: list[str] = []
    _drain_workflow_results(_FakeConsole(), registry=reg, sink=sink)
    assert sink == []  # sync runs already returned their summary as the tool result


# --- config: new fields ---------------------------------------------------


def test_parse_workflow_config_new_fields():
    from bareagent.main import _parse_workflow_config

    cfg = _parse_workflow_config({"default_token_budget": 5000, "max_runs": 10})
    assert cfg.default_token_budget == 5000
    assert cfg.max_runs == 10


def test_parse_workflow_config_new_fields_defaults_and_bad_values():
    from bareagent.core.workflow_registry import DEFAULT_MAX_RUNS
    from bareagent.main import WorkflowConfig, _parse_workflow_config

    defaults = WorkflowConfig()
    assert _parse_workflow_config({}).default_token_budget == defaults.default_token_budget
    assert _parse_workflow_config({}).max_runs == DEFAULT_MAX_RUNS
    # negative budget -> default (0); max_runs < 1 -> default.
    assert _parse_workflow_config({"default_token_budget": -5}).default_token_budget == 0
    assert _parse_workflow_config({"max_runs": 0}).max_runs == DEFAULT_MAX_RUNS


def test_parse_workflow_config_env_overrides(monkeypatch):
    from bareagent.main import _parse_workflow_config

    monkeypatch.setenv("BAREAGENT_WORKFLOW_DEFAULT_TOKEN_BUDGET", "12345")
    monkeypatch.setenv("BAREAGENT_WORKFLOW_MAX_RUNS", "7")
    cfg = _parse_workflow_config({})
    assert cfg.default_token_budget == 12345
    assert cfg.max_runs == 7
