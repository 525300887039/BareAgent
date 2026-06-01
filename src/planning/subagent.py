from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any

from src.concurrency.background import BackgroundManager
from src.core.fileutil import generate_random_id
from src.core.loop import agent_loop
from src.memory.compact import Compactor
from src.permission.guard import PermissionGuard
from src.planning.agent_types import (
    BUILTIN_AGENT_TYPES,
    DEFAULT_AGENT_TYPE,
    AgentType,
    filter_handlers,
    filter_tools,
    resolve_agent_type,
)
from src.planning.worktree import (
    WorktreeError,
    WorktreeHandle,
    create_worktree,
    is_git_repo,
    remove_worktree,
    worktree_status,
)
from src.provider.base import BaseLLMProvider
from src.tracing import tracer as global_tracer

_SUBAGENT_COMPACT_THRESHOLD = 50_000

# Memory commands that mutate the store. Read-only agent types may only
# ``view``; the rest are rejected by ``_make_readonly_memory_handler``.
_MEMORY_WRITE_COMMANDS = frozenset({"create", "str_replace", "insert", "delete", "rename"})


def _make_readonly_memory_handler(inner: Any) -> Any:
    """Wrap a ``memory`` handler so write commands are refused.

    The ``memory`` tool is a single tool with a ``command`` enum, so it cannot
    be removed by name-filtering the way ``mcp__*`` / ``lsp_*`` tools are.
    Instead we downgrade it here, at the boundary where the child agent type is
    known, leaving ``view`` available for recall.
    """

    def _wrapped(**kwargs: Any) -> Any:
        command = str(kwargs.get("command", "")).strip()
        if command in _MEMORY_WRITE_COMMANDS:
            return (
                "Error: memory is read-only for this agent type; only the "
                "'view' command is permitted."
            )
        return inner(**kwargs)

    return _wrapped


def _build_subagent_description() -> str:
    lines = [
        "Delegate a self-contained task to a child agent with isolated messages.",
        "Available agent types:",
    ]
    for name, at in BUILTIN_AGENT_TYPES.items():
        lines.append(f"- {name}: {at.description}")
    return "\n".join(lines)


SUBAGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "subagent",
        "description": _build_subagent_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to complete.",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Optional child-agent profile to use.",
                    "enum": list(BUILTIN_AGENT_TYPES.keys()),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run the child agent asynchronously in the background.",
                    "default": False,
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree"],
                    "default": "none",
                    "description": (
                        "Set 'worktree' to run the child agent in an isolated git "
                        "worktree + temp branch; file ops won't touch the main working tree."
                    ),
                },
            },
            "required": ["task"],
        },
    }
]


def run_subagent(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str = "",
    max_depth: int = 3,
    current_depth: int = 1,
    agent_type: str | None = None,
    bg_manager: BackgroundManager | None = None,
    run_in_background: bool = False,
    default_agent_type: str = DEFAULT_AGENT_TYPE,
    isolation: str = "none",
    retry_policy: Any = None,
) -> str:
    if current_depth > max_depth:
        return f"Subagent refused: recursion depth {current_depth} exceeds limit {max_depth}."

    resolved_type = resolve_agent_type(agent_type, default_name=default_agent_type)
    child_permission = _build_child_permission(
        permission=permission,
        agent_type=resolved_type,
        background=run_in_background,
    )

    if run_in_background:
        if bg_manager is None:
            return (
                "Subagent background execution unavailable: background manager is not configured."
            )
        task_id = _generate_subagent_task_id()
        bg_manager.submit(
            task_id,
            partial(
                _run_subagent_sync,
                provider=provider,
                task=task,
                tools=tools,
                handlers=handlers,
                permission=child_permission,
                system_prompt=system_prompt,
                max_depth=max_depth,
                current_depth=current_depth,
                resolved_type=resolved_type,
                bg_manager=bg_manager,
                default_agent_type=default_agent_type,
                isolation=isolation,
                retry_policy=retry_policy,
            ),
        )
        return f"Subagent {task_id} started in the background."

    return _run_subagent_sync(
        provider=provider,
        task=task,
        tools=tools,
        handlers=handlers,
        permission=child_permission,
        system_prompt=system_prompt,
        max_depth=max_depth,
        current_depth=current_depth,
        resolved_type=resolved_type,
        bg_manager=bg_manager,
        default_agent_type=default_agent_type,
        isolation=isolation,
        retry_policy=retry_policy,
    )


def _run_subagent_sync(
    provider: BaseLLMProvider,
    task: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: Any,
    system_prompt: str,
    max_depth: int,
    current_depth: int,
    resolved_type: AgentType,
    bg_manager: BackgroundManager | None,
    default_agent_type: str,
    isolation: str = "none",
    retry_policy: Any = None,
) -> str:
    filtered_tools = filter_tools(tools, resolved_type)
    child_handlers = filter_handlers(handlers, filtered_tools)
    if not resolved_type.memory_writable and "memory" in child_handlers:
        child_handlers["memory"] = _make_readonly_memory_handler(child_handlers["memory"])
    resolved_system_prompt = _compose_system_prompt(
        parent_prompt=system_prompt,
        agent_prompt=resolved_type.system_prompt,
    )
    compact_fn = Compactor(
        provider=provider,
        transcript_mgr=None,
        threshold=_SUBAGENT_COMPACT_THRESHOLD,
    )

    # Worktree isolation: rebind the six file-op handlers onto the worktree
    # path *before* the nested subagent closure is built, so a child spawned
    # inside this worktree also writes into the worktree. fail-open: a non-git
    # workspace or a failed worktree creation falls back to no isolation with a
    # footnote (isolation is a convenience, PermissionGuard is the safety edge).
    handle: WorktreeHandle | None = None
    footnote = ""
    if isolation == "worktree":
        base = os.getcwd()
        if not is_git_repo(base):
            footnote = "\n\n[worktree] skipped: not a git repository (ran without isolation)."
        else:
            try:
                handle = create_worktree(base)
            except WorktreeError as exc:
                footnote = f"\n\n[worktree] skipped: {exc} (ran without isolation)."
            else:
                # Lazy import: ``src.core.tools`` imports this module at top
                # level, so importing it here breaks the cycle.
                from src.core.tools import rebind_workspace_handlers

                child_handlers = rebind_workspace_handlers(child_handlers, Path(handle.path))

    if "subagent" in child_handlers:
        # Capture the (possibly rebound) child_handlers so a nested subagent
        # inherits the worktree-rooted file handlers. Nested isolation defaults
        # to "none" (no worktree-in-worktree, per Out of Scope).
        nested_handlers = child_handlers
        child_handlers["subagent"] = (
            lambda task, agent_type=None, run_in_background=False, isolation="none": run_subagent(
                provider=provider,
                task=task,
                tools=filtered_tools,
                handlers=nested_handlers,
                permission=permission,
                system_prompt=resolved_system_prompt,
                max_depth=max_depth,
                current_depth=current_depth + 1,
                agent_type=agent_type,
                bg_manager=bg_manager,
                run_in_background=run_in_background,
                default_agent_type=default_agent_type,
                isolation=isolation,
                retry_policy=retry_policy,
            )
        )

    messages: list[dict[str, Any]] = []
    if resolved_system_prompt.strip():
        messages.append({"role": "system", "content": resolved_system_prompt})
    messages.append({"role": "user", "content": task})
    try:
        with global_tracer.trace(
            "subagent",
            tags={"agent_type": resolved_type.name, "depth": current_depth},
        ) as span:
            span.set_content_tag("task", task)
            result = agent_loop(
                provider=provider,
                messages=messages,
                tools=filtered_tools,
                handlers=child_handlers,
                permission=permission,
                compact_fn=compact_fn,
                bg_manager=None,
                max_iterations=resolved_type.max_turns,
                retry_policy=retry_policy,
            )
            span.set_content_tag("result", result[:500])
    finally:
        if handle is not None:
            footnote = _finalize_worktree(handle)
    return result + footnote


def _finalize_worktree(handle: WorktreeHandle) -> str:
    """Keep a dirty worktree (with a report) or clean up a pristine one.

    Returns the footnote to append to the sub-agent's result.
    """
    dirty, summary = worktree_status(handle.path)
    if dirty:
        return (
            f"\n\n[worktree] kept at {handle.path} on branch {handle.branch} "
            f"({summary}). Inspect with: git worktree list."
        )
    remove_worktree(handle)
    return f"\n\n[worktree] cleaned up (no changes) at branch {handle.branch}."


def _compose_system_prompt(*, parent_prompt: str, agent_prompt: str) -> str:
    parts = [p for raw in (parent_prompt, agent_prompt) if (p := raw.strip())]
    return "\n\n".join(parts)


def _build_child_permission(
    *,
    permission: Any,
    agent_type: AgentType,
    background: bool,
) -> Any:
    if isinstance(permission, PermissionGuard):
        return permission.for_subagent(agent_type, background=background)
    return permission


def _generate_subagent_task_id() -> str:
    return "subagent-" + generate_random_id(8)
