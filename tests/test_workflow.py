from __future__ import annotations

import pytest

from src.core.handlers.workflow import WORKFLOW_TOOL_SCHEMA, run_workflow_tool
from src.core.workflow import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_NODES,
    NodeResult,
    NodeStatus,
    WorkflowError,
    WorkflowNode,
    WorkflowSpec,
    build_node_prompt,
    compute_ready,
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
    from src.core.tools import get_tools

    names = {tool["name"] for tool in get_tools()}
    assert "workflow" not in names  # main-loop-only; never offered globally


def test_workflow_stripped_for_every_subagent_type():
    from src.planning.agent_types import BUILTIN_AGENT_TYPES, filter_tools

    fake_loop_tools = [WORKFLOW_TOOL_SCHEMA, {"name": "read_file"}]
    for agent_type in BUILTIN_AGENT_TYPES.values():
        kept = {tool["name"] for tool in filter_tools(fake_loop_tools, agent_type)}
        assert "workflow" not in kept, agent_type.name


# --- main.py wiring: config + thread-pool batch ---------------------------


def test_parse_workflow_config_defaults():
    from src.main import WorkflowConfig, _parse_workflow_config

    cfg = _parse_workflow_config({})
    assert cfg == WorkflowConfig()
    assert cfg.enabled is True
    assert cfg.max_concurrency == DEFAULT_MAX_CONCURRENCY
    assert cfg.max_nodes == DEFAULT_MAX_NODES


def test_parse_workflow_config_values():
    from src.main import _parse_workflow_config

    cfg = _parse_workflow_config({"enabled": False, "max_concurrency": 3, "max_nodes": 50})
    assert cfg.enabled is False
    assert cfg.max_concurrency == 3
    assert cfg.max_nodes == 50


def test_parse_workflow_config_bad_values_fall_back():
    from src.main import _parse_workflow_config

    assert _parse_workflow_config({"max_concurrency": "nope"}).max_concurrency == (
        DEFAULT_MAX_CONCURRENCY
    )
    assert _parse_workflow_config({"max_concurrency": 0}).max_concurrency == DEFAULT_MAX_CONCURRENCY
    assert _parse_workflow_config({"max_nodes": -1}).max_nodes == DEFAULT_MAX_NODES


def test_parse_workflow_config_env_override(monkeypatch):
    from src.main import _parse_workflow_config

    monkeypatch.setenv("BAREAGENT_WORKFLOW_ENABLED", "false")
    assert _parse_workflow_config({"enabled": True}).enabled is False


def test_run_node_batch_preserves_order():
    from src.main import _run_node_batch

    def make(value: int):
        return lambda: NodeResult(id=str(value), status=NodeStatus.DONE, output=str(value))

    thunks = [make(i) for i in range(5)]
    results = _run_node_batch(thunks, max_concurrency=4)
    assert [r.id for r in results] == ["0", "1", "2", "3", "4"]


def test_run_node_batch_empty_and_single():
    from src.main import _run_node_batch

    assert _run_node_batch([], max_concurrency=4) == []
    single = _run_node_batch(
        [lambda: NodeResult(id="x", status=NodeStatus.DONE)], max_concurrency=4
    )
    assert [r.id for r in single] == ["x"]


def test_install_workflow_handler_respects_enabled_flag():
    from src.main import _install_workflow_handler

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
    )
    disabled: dict = {}
    _install_workflow_handler(disabled, enabled=False, **common)
    assert "workflow" not in disabled  # short-circuit: tool never installed

    enabled: dict = {}
    _install_workflow_handler(enabled, enabled=True, **common)
    assert callable(enabled.get("workflow"))
