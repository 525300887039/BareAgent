"""Deterministic workflow orchestration: run a static DAG of subagent nodes.

Pure logic with no LLM / loop / threading / SDK dependencies, so DAG parsing,
validation (cycle detection / dangling deps / limits), the ready-set scheduler,
result threading, and summary formatting are all unit-testable with injected
callbacks (mirrors the ``src/core/goal.py`` and ``src/core/retry.py`` pure-module
pattern).

Division of labor (see task 06-06-workflow-deterministic-orchestration):
- This module owns the *control flow*: which nodes are ready to run given what
  has completed, how a failed node skips its transitive dependents, how upstream
  results are threaded into a downstream prompt, and how the final summary reads
  (``run_workflow`` + the pure helpers).
- The REPL (``main.py``) owns the side-effecting parts: executing one node as an
  isolated ``run_subagent`` call and running a batch of ready nodes concurrently
  on a thread pool. These are injected into :func:`run_workflow` as the
  ``execute_node`` / ``map_concurrent`` callbacks.

The LLM authors the DAG on the fly via the isolated ``workflow`` tool (declarative
nodes + ``depends_on`` edges, NOT executable code), keeping orchestration
deterministic and free of any code-``exec`` sandbox. Loops / conditionals /
dynamic fan-out are intentionally out of scope (static DAG); the model can issue
another ``workflow`` call from the main loop when it needs to branch.

Execution is *layered*: each iteration runs the whole current ready set
concurrently and waits for it before recomputing the next ready set. A node
therefore waits for all of its layer peers, not just its own dependencies -- a
known MVP simplification (a continuous as-completed scheduler is a later
extension). Failure semantics are *fail-soft* (decision (b)): a node whose
executor raises becomes ``FAILED``; its transitive dependents become ``SKIPPED``;
independent branches keep running.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Default ceiling on how many nodes one workflow may declare; guards against an
# LLM emitting a pathologically large DAG that floods the thread pool. Override
# via ``[workflow] max_nodes``.
DEFAULT_MAX_NODES = 20
# Default cap on concurrently-running nodes. Conservative because each node is a
# full subagent that may itself spawn work. Override via ``[workflow]
# max_concurrency``.
DEFAULT_MAX_CONCURRENCY = 8

# Matches ``{{ node_id }}`` placeholders in a node prompt for upstream-result
# substitution. Ids are restricted to a safe identifier-ish charset.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


class WorkflowError(Exception):
    """Raised when workflow input is structurally unusable (not a node list)."""


class NodeStatus(Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class WorkflowNode:
    """One unit of work in the DAG: an isolated subagent task.

    ``depends_on`` lists the ids whose outputs must complete (DONE) before this
    node runs. ``phase`` / ``label`` are organizational metadata surfaced in the
    summary; they do not affect scheduling.
    """

    id: str
    prompt: str
    agent_type: str | None = None
    depends_on: list[str] = field(default_factory=list)
    phase: str | None = None
    label: str | None = None


@dataclass(slots=True)
class NodeResult:
    """Terminal (or in-flight PENDING) state of a node after the scheduler runs."""

    id: str
    status: NodeStatus
    output: str = ""
    error: str = ""


@dataclass(slots=True)
class WorkflowSpec:
    """A parsed (not yet validated) workflow DAG."""

    nodes: list[WorkflowNode]


def _coerce_node(raw: Any) -> WorkflowNode:
    """Coerce one raw node mapping into a :class:`WorkflowNode` defensively.

    A non-dict entry becomes an empty-id node so :func:`validate_workflow` reports
    it rather than crashing here. Missing optional fields fall back to ``None`` /
    ``[]``.
    """
    if not isinstance(raw, dict):
        return WorkflowNode(id="", prompt="")

    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    depends_raw = raw.get("depends_on")
    if isinstance(depends_raw, list):
        depends_on = [str(d).strip() for d in depends_raw if str(d).strip()]
    elif isinstance(depends_raw, str) and depends_raw.strip():
        depends_on = [depends_raw.strip()]
    else:
        depends_on = []

    return WorkflowNode(
        id=str(raw.get("id", "") or "").strip(),
        prompt=str(raw.get("prompt", "") or ""),
        agent_type=_opt_str("agent_type"),
        depends_on=depends_on,
        phase=_opt_str("phase"),
        label=_opt_str("label"),
    )


def parse_workflow(tool_input: Any) -> WorkflowSpec:
    """Parse a ``workflow`` tool input into a :class:`WorkflowSpec`.

    Raises :class:`WorkflowError` only for a fundamentally unusable shape (no
    ``nodes`` array). Per-node coercion is lenient; semantic problems (empty id,
    dangling dep, cycle) are reported by :func:`validate_workflow`.
    """
    if not isinstance(tool_input, dict):
        raise WorkflowError("workflow input must be an object with a 'nodes' array.")
    raw_nodes = tool_input.get("nodes")
    if not isinstance(raw_nodes, list):
        raise WorkflowError("workflow 'nodes' must be an array.")
    return WorkflowSpec(nodes=[_coerce_node(raw) for raw in raw_nodes])


def _find_cycle(nodes: list[WorkflowNode], valid_ids: set[str]) -> list[str] | None:
    """Return a node-id cycle path (``a -> b -> a``) if the DAG has one, else None.

    Only edges to existing ids are considered (dangling deps are reported
    separately), and self-loops are ignored here (also reported separately).
    """
    graph: dict[str, list[str]] = {
        n.id: [d for d in n.depends_on if d in valid_ids and d != n.id] for n in nodes if n.id
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    path: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        color[node_id] = GRAY
        path.append(node_id)
        for dep in graph.get(node_id, []):
            dep_color = color.get(dep, BLACK)
            if dep_color == GRAY:
                return path[path.index(dep) :] + [dep]
            if dep_color == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        path.pop()
        color[node_id] = BLACK
        return None

    for node_id in graph:
        if color[node_id] == WHITE:
            found = visit(node_id)
            if found is not None:
                return found
    return None


def validate_workflow(spec: WorkflowSpec, *, max_nodes: int = DEFAULT_MAX_NODES) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    Checks: at least one node, node count within ``max_nodes``, non-empty unique
    ids, non-empty prompts, no self-dependency, every ``depends_on`` references an
    existing node, and the dependency graph is acyclic.
    """
    errors: list[str] = []
    nodes = spec.nodes
    if not nodes:
        errors.append("workflow must contain at least one node.")
        return errors
    if max_nodes > 0 and len(nodes) > max_nodes:
        errors.append(f"workflow has {len(nodes)} nodes, exceeding the limit of {max_nodes}.")

    seen: set[str] = set()
    duplicates: set[str] = set()
    has_blank_id = False
    for node in nodes:
        if not node.id:
            has_blank_id = True
        elif node.id in seen:
            duplicates.add(node.id)
        else:
            seen.add(node.id)
    if has_blank_id:
        errors.append("every node must have a non-empty 'id'.")
    for dup in sorted(duplicates):
        errors.append(f"duplicate node id: {dup!r}.")

    for node in nodes:
        if not node.prompt.strip():
            errors.append(f"node {node.id!r} has an empty 'prompt'.")
        for dep in node.depends_on:
            if dep == node.id:
                errors.append(f"node {node.id!r} cannot depend on itself.")
            elif dep not in seen:
                errors.append(f"node {node.id!r} depends on unknown node {dep!r}.")

    cycle = _find_cycle(nodes, seen)
    if cycle is not None:
        errors.append("workflow has a dependency cycle: " + " -> ".join(cycle))
    return errors


def compute_ready(spec: WorkflowSpec, results: dict[str, NodeResult]) -> list[WorkflowNode]:
    """Return PENDING nodes whose every dependency is DONE (runnable now)."""
    ready: list[WorkflowNode] = []
    for node in spec.nodes:
        if results[node.id].status is not NodeStatus.PENDING:
            continue
        if all(results[dep].status is NodeStatus.DONE for dep in node.depends_on):
            ready.append(node)
    return ready


def propagate_skips(spec: WorkflowSpec, results: dict[str, NodeResult]) -> set[str]:
    """Mark every PENDING node with a FAILED/SKIPPED dependency as SKIPPED.

    Runs to a fixpoint so skips cascade transitively. Returns the set of newly
    skipped ids. Assumes a validated spec (all ``depends_on`` ids exist in
    ``results``).
    """
    newly_skipped: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in spec.nodes:
            if results[node.id].status is not NodeStatus.PENDING:
                continue
            if any(
                results[dep].status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
                for dep in node.depends_on
            ):
                results[node.id] = NodeResult(
                    id=node.id,
                    status=NodeStatus.SKIPPED,
                    error="upstream dependency failed or was skipped",
                )
                newly_skipped.add(node.id)
                changed = True
    return newly_skipped


def build_node_prompt(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
    """Build a node's prompt, threading in its upstream dependency outputs.

    ``{{dep_id}}`` placeholders are replaced with that dependency's output text.
    Any dependency not referenced by a placeholder is appended verbatim under an
    "Upstream results" section so the node always sees what it depends on. An
    unknown placeholder (not a declared dependency) is left untouched.
    """
    used: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        result = upstream.get(key)
        if result is None:
            return match.group(0)
        used.add(key)
        return result.output

    prompt = _PLACEHOLDER.sub(_sub, node.prompt)

    extras = [result for dep_id, result in upstream.items() if dep_id not in used]
    if extras:
        parts = [prompt.rstrip(), "", "# Upstream results"]
        for result in extras:
            parts.append(f'\n<result from="{result.id}">\n{result.output.strip()}\n</result>')
        prompt = "\n".join(parts)
    return prompt


def format_summary(spec: WorkflowSpec, results: dict[str, NodeResult]) -> str:
    """Render the aggregated, structured workflow result fed back to the LLM."""
    done = sum(1 for n in spec.nodes if results[n.id].status is NodeStatus.DONE)
    failed = sum(1 for n in spec.nodes if results[n.id].status is NodeStatus.FAILED)
    skipped = sum(1 for n in spec.nodes if results[n.id].status is NodeStatus.SKIPPED)

    blocks = [
        f"Workflow finished: {done} done, {failed} failed, {skipped} skipped "
        f"(of {len(spec.nodes)} nodes)."
    ]
    for node in spec.nodes:
        result = results[node.id]
        title = f"## [{result.status.value}] {node.id}"
        if node.phase:
            title += f" (phase: {node.phase})"
        if node.label:
            title += f" - {node.label}"
        blocks.append(title)
        if result.status is NodeStatus.DONE:
            blocks.append(result.output.strip() or "(no output)")
        elif result.status is NodeStatus.FAILED:
            blocks.append(f"Error: {result.error}")
        else:
            blocks.append(result.error or "Skipped.")
    return "\n\n".join(blocks)


def _make_node_thunk(
    node: WorkflowNode,
    results: dict[str, NodeResult],
    execute_node: Callable[[WorkflowNode, dict[str, NodeResult]], Any],
) -> Callable[[], NodeResult]:
    """Build a total (never-raising) thunk that runs one node and returns a result.

    The upstream snapshot is taken at thunk-build time (deps are already DONE).
    The executor's exceptions become a FAILED result -- failure is data the
    scheduler propagates, not control flow that aborts the batch.
    """
    upstream = {dep: results[dep] for dep in node.depends_on}

    def _thunk() -> NodeResult:
        try:
            output = execute_node(node, upstream)
        except Exception as exc:  # noqa: BLE001 - any node failure is fail-soft
            return NodeResult(
                id=node.id,
                status=NodeStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        return NodeResult(id=node.id, status=NodeStatus.DONE, output=str(output))

    return _thunk


def run_workflow(
    spec: WorkflowSpec,
    *,
    execute_node: Callable[[WorkflowNode, dict[str, NodeResult]], Any],
    map_concurrent: Callable[[list[Callable[[], NodeResult]]], list[NodeResult]],
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, NodeResult]:
    """Drive a validated DAG to completion, returning each node's terminal result.

    - ``execute_node(node, upstream)`` runs one node and returns its output text;
      raising marks the node FAILED (its dependents become SKIPPED).
    - ``map_concurrent(thunks)`` runs a batch of ready-node thunks (which never
      raise) concurrently and returns their results in order. Tests inject a
      synchronous map; ``main.py`` injects a thread-pool-backed one.
    - ``on_progress`` receives human-readable progress lines (main thread only).

    Must be called on a spec that passed :func:`validate_workflow` (acyclic, all
    deps resolved), otherwise the scheduler could stall.
    """
    results = {node.id: NodeResult(id=node.id, status=NodeStatus.PENDING) for node in spec.nodes}

    def emit(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    while True:
        propagate_skips(spec, results)
        ready = compute_ready(spec, results)
        if not ready:
            break
        emit("Running " + str(len(ready)) + " node(s): " + ", ".join(n.id for n in ready))
        batch = map_concurrent([_make_node_thunk(node, results, execute_node) for node in ready])
        for result in batch:
            results[result.id] = result
            if result.status is NodeStatus.DONE:
                emit(f"  done: {result.id}")
            else:
                emit(f"  {result.status.value}: {result.id} ({result.error})")
    return results
