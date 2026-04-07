from __future__ import annotations

import threading
from functools import partial
from pathlib import Path
from typing import Any, Callable

from src.concurrency.background import BackgroundManager
from src.core.fileutil import generate_random_id
from src.core.handlers.bash import run_bash
from src.core.handlers.file_edit import run_edit
from src.core.handlers.file_read import run_read
from src.core.handlers.file_write import run_write
from src.core.handlers.glob_search import run_glob
from src.core.handlers.grep_search import run_grep
from src.core.schema import tool_schema as _schema
from src.planning.skills import (
    LOAD_SKILL_TOOL_SCHEMAS,
    SkillLoader,
    make_skill_handlers,
    resolve_skills_dir,
)
from src.planning.subagent import SUBAGENT_TOOL_SCHEMAS, run_subagent
from src.planning.tasks import TASK_TOOL_SCHEMAS, TaskManager, make_task_handlers
from src.planning.todo import TODO_TOOL_SCHEMAS, TodoManager, make_todo_handlers

BASE_TOOLS = {"bash", "read_file", "write_file", "edit_file", "glob", "grep"}
DEFERRED_TOOLS = {
    "todo_write",
    "todo_read",
    "subagent",
    "load_skill",
    "task_create",
    "task_list",
    "task_get",
    "task_update",
    "background_run",
    "team_spawn",
    "team_send",
    "team_list",
}


BACKGROUND_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "background_run",
        "Run a shell command in a daemon thread and report the result later.",
        {
            "command": {
                "type": "string",
                "description": "Shell command to execute in the background.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds.",
                "default": 30,
                "minimum": 1,
            },
            "task_id": {
                "type": "string",
                "description": "Optional background task id. Auto-generated when omitted.",
            },
        },
        ["command"],
    ),
]
TEAM_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "team_spawn",
        "Spawn a registered teammate as an autonomous background agent.",
        {
            "name": {
                "type": "string",
                "description": "Registered teammate name.",
            }
        },
        ["name"],
    ),
    _schema(
        "team_send",
        "Send a message to a teammate mailbox.",
        {
            "to_agent": {
                "type": "string",
                "description": "Target teammate name.",
            },
            "content": {
                "type": "string",
                "description": "Message content.",
            },
        },
        ["to_agent", "content"],
    ),
    _schema(
        "team_list",
        "List registered teammates.",
        {},
        [],
    ),
]
DEFERRED_TOOL_SCHEMAS: list[dict[str, Any]] = [
    *TODO_TOOL_SCHEMAS,
    *SUBAGENT_TOOL_SCHEMAS,
    *LOAD_SKILL_TOOL_SCHEMAS,
    *TASK_TOOL_SCHEMAS,
    *BACKGROUND_TOOL_SCHEMAS,
    *TEAM_TOOL_SCHEMAS,
]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "bash",
        "Run a shell command in the current workspace.",
        {
            "command": {"type": "string", "description": "Command to execute."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds.",
                "default": 30,
                "minimum": 1,
            },
        },
        ["command"],
    ),
    _schema(
        "read_file",
        "Read a UTF-8 text file with line numbers.",
        {
            "file_path": {"type": "string", "description": "Path to the file."},
            "offset": {
                "type": "integer",
                "description": "Zero-based line offset.",
                "default": 0,
                "minimum": 0,
            },
            "limit": {
                "type": ["integer", "null"],
                "description": "Maximum number of lines to read.",
                "default": None,
                "minimum": 0,
            },
        },
        ["file_path"],
    ),
    _schema(
        "write_file",
        "Write content to a text file inside the workspace.",
        {
            "file_path": {"type": "string", "description": "Path to the file."},
            "content": {"type": "string", "description": "Content to write."},
        },
        ["file_path", "content"],
    ),
    _schema(
        "edit_file",
        "Replace existing text in a workspace file.",
        {
            "file_path": {"type": "string", "description": "Path to the file."},
            "old_text": {
                "type": "string",
                "description": "Existing text to replace.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text.",
            },
        },
        ["file_path", "old_text", "new_text"],
    ),
    _schema(
        "glob",
        "Find files by glob pattern within the workspace.",
        {
            "pattern": {"type": "string", "description": "Glob pattern to match."},
            "path": {
                "type": "string",
                "description": "Directory to search from.",
                "default": ".",
            },
        },
        ["pattern"],
    ),
    _schema(
        "grep",
        "Search file contents with a regular expression.",
        {
            "pattern": {"type": "string", "description": "Regex to search for."},
            "path": {
                "type": "string",
                "description": "Path to search from.",
                "default": ".",
            },
            "include": {
                "type": "string",
                "description": "Optional glob filter for files.",
                "default": "",
            },
        },
        ["pattern"],
    ),
    *DEFERRED_TOOL_SCHEMAS,
]


def _make_lazy_task_handlers(task_file: Path) -> dict[str, Callable[..., Any]]:
    state: dict[str, dict[str, Callable[..., Any]] | None] = {"handlers": None}

    def _get_handlers() -> dict[str, Callable[..., Any]]:
        handlers = state["handlers"]
        if handlers is None:
            handlers = make_task_handlers(TaskManager(task_file))
            state["handlers"] = handlers
        return handlers

    return {
        "task_create": lambda title, description="", depends_on=None: _get_handlers()["task_create"](
            title=title,
            description=description,
            depends_on=depends_on,
        ),
        "task_update": lambda task_id, status=None, title=None: _get_handlers()["task_update"](
            task_id=task_id,
            status=status,
            title=title,
        ),
        "task_get": lambda task_id: _get_handlers()["task_get"](task_id=task_id),
        "task_list": lambda status=None: _get_handlers()["task_list"](status=status),
    }


_DEFAULT_TODO_MANAGER: TodoManager | None = None
_DEFAULT_SKILL_LOADER: SkillLoader | None = None
_SINGLETON_LOCK = threading.Lock()


def _get_default_todo_manager() -> TodoManager:
    global _DEFAULT_TODO_MANAGER
    if _DEFAULT_TODO_MANAGER is None:
        with _SINGLETON_LOCK:
            if _DEFAULT_TODO_MANAGER is None:
                _DEFAULT_TODO_MANAGER = TodoManager()
    return _DEFAULT_TODO_MANAGER


def _get_default_skill_loader() -> SkillLoader:
    global _DEFAULT_SKILL_LOADER
    if _DEFAULT_SKILL_LOADER is None:
        with _SINGLETON_LOCK:
            if _DEFAULT_SKILL_LOADER is None:
                _DEFAULT_SKILL_LOADER = SkillLoader(resolve_skills_dir())
    return _DEFAULT_SKILL_LOADER

def _unbound_stub(tool_name: str) -> Callable[..., Any]:
    """Raise when a file/bash handler is called without workspace binding."""
    def _stub(**_: Any) -> str:
        raise RuntimeError(f"{tool_name}: use get_handlers() with a workspace binding")
    return _stub


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "bash": _unbound_stub("bash"),
    "read_file": _unbound_stub("read_file"),
    "write_file": _unbound_stub("write_file"),
    "edit_file": _unbound_stub("edit_file"),
    "glob": _unbound_stub("glob"),
    "grep": _unbound_stub("grep"),
    "todo_read": lambda: make_todo_handlers(_get_default_todo_manager())["todo_read"](),
    "todo_write": lambda **kw: make_todo_handlers(_get_default_todo_manager())["todo_write"](**kw),
    **_make_lazy_task_handlers(Path(".tasks.json")),
    "load_skill": lambda skill_name: make_skill_handlers(_get_default_skill_loader())["load_skill"](skill_name),
    "background_run": lambda **_: "Background manager unavailable.",
    "subagent": (
        lambda task, agent_type=None, run_in_background=False: (
            "Subagent unavailable: provider is not configured."
        )
    ),
    "team_spawn": lambda name: f"Team spawning unavailable for {name}.",
    "team_send": lambda to_agent, content: f"Team messaging unavailable for {to_agent}.",
    "team_list": lambda: [],
}


def get_tools() -> list[dict[str, Any]]:
    return list(TOOL_SCHEMAS)


def get_handlers(
    workspace: Path,
    *,
    todo_manager: TodoManager | None = None,
    task_manager: TaskManager | None = None,
    skill_loader: SkillLoader | None = None,
    provider: Any = None,
    tools: list[dict[str, Any]] | None = None,
    permission: Any = None,
    bg_manager: BackgroundManager | None = None,
    subagent_system_prompt: str = "",
    subagent_max_depth: int = 3,
    subagent_default_type: str = "general-purpose",
    team_handlers: dict[str, Callable[..., Any]] | None = None,
    subagent_depth: int = 0,
) -> dict[str, Callable[..., Any]]:
    handlers: dict[str, Callable[..., Any]] = {
        "bash": partial(run_bash, cwd=workspace),
        "read_file": partial(run_read, workspace=workspace),
        "write_file": partial(run_write, workspace=workspace),
        "edit_file": partial(run_edit, workspace=workspace),
        "glob": partial(run_glob, workspace=workspace),
        "grep": partial(run_grep, workspace=workspace),
    }

    active_todo_manager = todo_manager or TodoManager()
    active_skill_loader = skill_loader or SkillLoader(resolve_skills_dir())
    handlers.update(make_todo_handlers(active_todo_manager))
    if task_manager is None:
        handlers.update(_make_lazy_task_handlers(workspace / ".tasks.json"))
    else:
        handlers.update(make_task_handlers(task_manager))
    handlers.update(make_skill_handlers(active_skill_loader))
    handlers["background_run"] = _make_background_run_handler(
        bg_manager=bg_manager,
        workspace=workspace,
    )

    handlers.update(
        team_handlers
        or {
            "team_spawn": lambda name: f"Team spawning unavailable for {name}.",
            "team_send": lambda to_agent, content: f"Team messaging unavailable for {to_agent}.",
            "team_list": lambda: [],
        }
    )

    available_tools = tools or get_tools()
    if provider is None:
        handlers["subagent"] = (
            lambda task, agent_type=None, run_in_background=False: (
                "Subagent unavailable: provider is not configured."
            )
        )
    else:
        handlers["subagent"] = lambda task, agent_type=None, run_in_background=False: run_subagent(
            provider=provider,
            task=task,
            tools=available_tools,
            handlers=handlers,
            permission=permission,
            system_prompt=subagent_system_prompt,
            max_depth=subagent_max_depth,
            current_depth=subagent_depth + 1,
            agent_type=agent_type,
            bg_manager=bg_manager,
            run_in_background=run_in_background,
            default_agent_type=subagent_default_type,
        )

    return handlers


def tool_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    _ = query, max_results
    return []


def _make_background_run_handler(
    *,
    bg_manager: BackgroundManager | None,
    workspace: Path,
) -> Callable[..., str]:
    bash_runner = partial(run_bash, cwd=workspace, raise_on_error=True)

    def _background_run(
        command: str,
        timeout: int = 30,
        task_id: str | None = None,
    ) -> str:
        if bg_manager is None:
            return "Background manager unavailable."

        candidate_id = task_id.strip() if isinstance(task_id, str) else ""
        resolved_task_id = candidate_id or _generate_background_task_id()
        bg_manager.submit(resolved_task_id, bash_runner, command, timeout)
        return f"Submitted background task {resolved_task_id}"

    return _background_run


def _generate_background_task_id() -> str:
    return generate_random_id(8)
