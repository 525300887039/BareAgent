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
shim: parse -> validate -> drive the pure :func:`src.core.workflow.run_workflow`
engine with the caller-injected ``execute_node`` / ``map_concurrent`` /
``on_progress`` callbacks -> format the aggregated summary. Returning an
``Error:`` string for bad input (rather than raising) keeps the main loop's tool
result clean (see ``error-handling.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.schema import tool_schema
from src.core.workflow import (
    DEFAULT_MAX_NODES,
    NodeResult,
    WorkflowError,
    WorkflowNode,
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
        "to branch on the results."
    ),
    {
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
        }
    },
    ["nodes"],
)


def run_workflow_tool(
    *,
    nodes: Any = None,
    execute_node: Callable[[WorkflowNode, dict[str, NodeResult]], Any],
    map_concurrent: Callable[[list[Callable[[], NodeResult]]], list[NodeResult]],
    on_progress: Callable[[str], None] | None = None,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> str:
    """Parse, validate, run, and summarize an LLM-authored workflow DAG.

    Returns the structured summary on success, or an ``Error:`` string for an
    unusable / invalid DAG. ``execute_node`` / ``map_concurrent`` / ``on_progress``
    are injected by ``main.py`` (subagent execution + thread pool + console).
    """
    try:
        spec = parse_workflow({"nodes": nodes})
    except WorkflowError as exc:
        return f"Error: {exc}"

    errors = validate_workflow(spec, max_nodes=max_nodes)
    if errors:
        return "Error: invalid workflow:\n" + "\n".join(f"- {error}" for error in errors)

    results = run_workflow(
        spec,
        execute_node=execute_node,
        map_concurrent=map_concurrent,
        on_progress=on_progress,
    )
    return format_summary(spec, results)
