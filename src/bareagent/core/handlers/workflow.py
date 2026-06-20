"""Handler + schema for the ``workflow`` tool (deterministic DAG orchestration).

Like ``exit_plan_mode`` / ``goal_verdict`` / ``skill_create``, ``workflow`` is a
main-loop-only tool: its schema is NOT in the global ``get_tools()`` set, it is
appended only to the top-level ``loop_tools`` and its handler is installed on the
main loop's handler dict (see ``main.py``). ``"workflow"`` is also in
``agent_types.MAIN_LOOP_ONLY_TOOLS`` so ``filter_tools`` strips it from every
sub-agent type, and ``filter_handlers`` then drops the orphaned handler -- a
sub-agent can never fan out its own workflow (no nesting in the MVP).

The LLM authors the DAG on the fly: a ``nodes`` array of declarative subagent
tasks with ``depends_on`` edges (not executable code). The handler is a thin
shim: parse -> validate -> drive the pure :func:`bareagent.core.workflow.run_workflow`
engine with the caller-injected ``execute_node`` / ``map_concurrent`` /
``on_progress`` callbacks -> format the aggregated summary. Returning an
``Error:`` string for bad input (rather than raising) keeps the main loop's tool
result clean (see ``error-handling.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bareagent.core.schema import tool_schema
from bareagent.core.workflow import (
    DEFAULT_MAX_NODES,
    NodeResult,
    WorkflowError,
    WorkflowNode,
    WorkflowSpec,
    compute_resume_plan,
    format_summary,
    parse_workflow,
    run_workflow,
    validate_workflow,
)

WORKFLOW_TOOL_SCHEMA = tool_schema(
    "workflow",
    (
        "Run a deterministic DAG of subagent tasks in parallel. Provide 'nodes': "
        "each node is an isolated subagent given a 'prompt'; independent nodes run "
        "concurrently and nodes wait for their 'depends_on' nodes. Reference an "
        "upstream node's output in a prompt with {{node_id}}. Use this when you can "
        "decompose the work into independent or dependency-ordered pieces up front; "
        "it returns a structured summary of every node's result. The structure is "
        "fixed once submitted (no loops/conditionals); issue another workflow call "
        "to branch on the results. Set 'run_in_background' to get a run id back "
        "immediately and have the result delivered when it finishes (watch it with "
        "/workflows). Set 'resume_from' to a prior run id to reuse that run's "
        "unchanged completed nodes and only re-run changed/failed/new ones. Set "
        "'token_budget' to cap the run: once spent, remaining nodes are skipped."
    ),
    {
        "run_in_background": {
            "type": "boolean",
            "description": (
                "Run asynchronously: return a run id now and deliver the full "
                "summary when it finishes (default false = block and return it)."
            ),
        },
        "resume_from": {
            "type": "string",
            "description": (
                "A prior workflow run id. Nodes with the same id and an unchanged "
                "prompt that completed last run are reused (not re-executed); "
                "changed/failed/new nodes and everything downstream of them re-run. "
                "An unknown id is ignored (the whole DAG runs fresh)."
            ),
        },
        "token_budget": {
            "type": "integer",
            "description": (
                "Soft token ceiling for this run. Checked before each layer; once "
                "spent tokens reach it, remaining nodes are skipped. Omit/0 = no cap."
            ),
        },
        "nodes": {
            "type": "array",
            "description": "The DAG nodes. Ids must be unique and the graph acyclic.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique node id (referenced by depends_on and {{id}}).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The task for this node's subagent.",
                    },
                    "agent_type": {
                        "type": "string",
                        "description": (
                            "Optional subagent profile (e.g. general-purpose, explore, "
                            "plan, code-review). Defaults to the configured default."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ids of nodes whose outputs must complete first.",
                    },
                    "phase": {
                        "type": "string",
                        "description": "Optional label grouping nodes in the summary.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional short human-readable node label.",
                    },
                },
                "required": ["id", "prompt"],
            },
        },
    },
    ["nodes"],
)


def validate_workflow_input(
    nodes: Any, *, max_nodes: int = DEFAULT_MAX_NODES
) -> WorkflowSpec | str:
    """Parse + validate raw ``nodes`` into a spec, or return an ``Error:`` string.

    Split out so ``main.py`` can validate once up front (immediate feedback for a
    malformed DAG, even when ``run_in_background`` is set) and then reuse the
    parsed spec without re-parsing.
    """
    try:
        spec = parse_workflow({"nodes": nodes})
    except WorkflowError as exc:
        return f"Error: {exc}"
    errors = validate_workflow(spec, max_nodes=max_nodes)
    if errors:
        return "Error: invalid workflow:\n" + "\n".join(f"- {error}" for error in errors)
    return spec


def run_workflow_tool(
    *,
    nodes: Any = None,
    spec: WorkflowSpec | None = None,
    resume_from: str | None = None,
    token_budget: int = 0,
    execute_node: Callable[[WorkflowNode, dict[str, NodeResult]], Any],
    map_concurrent: Callable[[list[Callable[[], NodeResult]]], list[NodeResult]],
    on_progress: Callable[[str], None] | None = None,
    on_node_status: Callable[[str, NodeResult], None] | None = None,
    resolve_prior: Callable[[str], tuple[WorkflowSpec, dict[str, NodeResult]] | None] | None = None,
    tokens_spent: Callable[[], int] | None = None,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> str:
    """Resolve resume, run, and summarize an LLM-authored workflow DAG.

    Pass a pre-validated ``spec`` to skip parsing (``main.py`` does this so it can
    register the run before driving); otherwise raw ``nodes`` are parsed +
    validated here, returning an ``Error:`` string for an unusable DAG.

    Resume: when ``resume_from`` and ``resolve_prior`` are given and the prior run
    is found, :func:`compute_resume_plan` decides which nodes to reuse. An unknown
    id falls open to a fresh run. Budget: ``token_budget`` (>0) plus ``tokens_spent``
    stop the run at a layer boundary once spent. ``on_node_status`` mirrors each
    node's terminal state out to the registry for the ``/workflows`` panel.
    """
    if spec is None:
        validated = validate_workflow_input(nodes, max_nodes=max_nodes)
        if isinstance(validated, str):
            return validated
        spec = validated

    reused: dict[str, NodeResult] = {}
    if resume_from and resolve_prior is not None:
        prior = resolve_prior(resume_from)
        if prior is not None:
            prior_spec, prior_results = prior
            reused = compute_resume_plan(spec, prior_spec, prior_results)

    results = run_workflow(
        spec,
        execute_node=execute_node,
        map_concurrent=map_concurrent,
        on_progress=on_progress,
        on_node_status=on_node_status,
        reused_results=reused,
        token_budget=token_budget if token_budget and token_budget > 0 else 0,
        tokens_spent=tokens_spent,
    )
    return format_summary(spec, results)
