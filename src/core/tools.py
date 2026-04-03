from __future__ import annotations

from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any, Callable

from src.core.handlers.bash import run_bash
from src.core.handlers.file_edit import run_edit
from src.core.handlers.file_read import run_read
from src.core.handlers.file_write import run_write
from src.core.handlers.glob_search import run_glob
from src.core.handlers.grep_search import run_grep
from src.planning.skills import (
    LOAD_SKILL_TOOL_SCHEMAS,
    SkillLoader,
    make_skill_handlers,
    resolve_skills_dir,
)
from src.planning.subagent import SUBAGENT_TOOL_SCHEMAS, run_subagent
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
}
DEFERRED_TOOL_SCHEMAS: list[dict[str, Any]] = [
    *TODO_TOOL_SCHEMAS,
    *SUBAGENT_TOOL_SCHEMAS,
    *LOAD_SKILL_TOOL_SCHEMAS,
]


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "parameters": input_schema,
    }


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

_DEFAULT_TODO_MANAGER = TodoManager()
_DEFAULT_SKILL_LOADER = SkillLoader(resolve_skills_dir())

TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "grep": run_grep,
    **make_todo_handlers(_DEFAULT_TODO_MANAGER),
    **make_skill_handlers(_DEFAULT_SKILL_LOADER),
    "subagent": lambda task: "Subagent unavailable: provider is not configured.",
}


def get_tools() -> list[dict[str, Any]]:
    return deepcopy(TOOL_SCHEMAS)


def get_handlers(
    workspace: Path,
    *,
    todo_manager: TodoManager | None = None,
    skill_loader: SkillLoader | None = None,
    provider: Any = None,
    tools: list[dict[str, Any]] | None = None,
    permission: Any = None,
    subagent_system_prompt: str = "",
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
    handlers.update(make_skill_handlers(active_skill_loader))

    available_tools = tools or get_tools()
    if provider is None:
        handlers["subagent"] = (
            lambda task: "Subagent unavailable: provider is not configured."
        )
    else:
        handlers["subagent"] = lambda task: run_subagent(
            provider=provider,
            task=task,
            tools=available_tools,
            handlers=handlers,
            permission=permission,
            system_prompt=subagent_system_prompt,
        )

    return handlers


def tool_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    _ = query, max_results
    return []
