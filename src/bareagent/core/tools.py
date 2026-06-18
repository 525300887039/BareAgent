from __future__ import annotations

import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from bareagent.concurrency.background import BackgroundManager
from bareagent.core.fileutil import generate_random_id
from bareagent.core.handlers.bash import run_bash
from bareagent.core.handlers.file_edit import run_edit
from bareagent.core.handlers.file_read import run_read
from bareagent.core.handlers.file_write import run_write
from bareagent.core.handlers.glob_search import run_glob
from bareagent.core.handlers.grep_search import run_grep
from bareagent.core.handlers.memory import run_memory
from bareagent.core.handlers.web_fetch import run_web_fetch
from bareagent.core.handlers.web_search import run_web_search
from bareagent.core.schema import tool_schema as _schema
from bareagent.lsp.manager import LanguageServerManager
from bareagent.lsp.tools import (
    LSP_TOOL_SCHEMAS,
    SEMANTIC_RENAME_TOOL_NAME,
    SEMANTIC_RENAME_TOOL_SCHEMA,
    build_lsp_tools,
)
from bareagent.mcp.manager import MCPManager
from bareagent.mcp.registry import build_mcp_handlers, build_mcp_tool_schemas
from bareagent.memory.persistent import MemoryManager
from bareagent.planning.skills import (
    LOAD_SKILL_TOOL_SCHEMAS,
    SkillLoader,
    make_skill_handlers,
    resolve_skills_dir,
)
from bareagent.planning.subagent import SUBAGENT_TOOL_SCHEMAS, run_subagent
from bareagent.planning.tasks import TASK_TOOL_SCHEMAS, TaskManager, make_task_handlers
from bareagent.planning.todo import TODO_TOOL_SCHEMAS, TodoManager, make_todo_handlers

BASE_TOOLS = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "web_fetch",
    "web_search",
}
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
    "team_shutdown",
    "team_register",
    "team_request_review",
    "lsp_outline",
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
    "memory",
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
        (
            "Send a message to a running teammate and wait for its reply, which is "
            "returned to you. If the teammate is not running (or the target is the "
            "main agent), returns immediately without waiting."
        ),
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
        "List registered teammates and whether each is currently running.",
        {},
        [],
    ),
    _schema(
        "team_shutdown",
        "Stop a single running teammate (sends a shutdown signal to its mailbox).",
        {
            "name": {
                "type": "string",
                "description": "Teammate name to stop.",
            }
        },
        ["name"],
    ),
    _schema(
        "team_register",
        (
            "Register a new teammate definition (persisted to .team.json) so it can "
            "later be spawned with team_spawn. This only defines the teammate; it does "
            "not start it. Omit provider/model to inherit the session provider."
        ),
        {
            "name": {
                "type": "string",
                "description": "Unique teammate name.",
            },
            "role": {
                "type": "string",
                "description": "Short role label, e.g. 'code reviewer'.",
            },
            "system_prompt": {
                "type": "string",
                "description": "System prompt defining the teammate's behavior.",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional LLM provider name (e.g. 'openai', 'anthropic'). "
                    "Inherits the session provider when omitted."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional model id. Inherits the session model when omitted.",
            },
        },
        ["name", "role", "system_prompt"],
    ),
    _schema(
        "team_request_review",
        (
            "Send a plan or proposal to a running teammate for approval and wait for "
            "its verdict, which is returned to you. If the teammate is not running (or "
            "the target is the main agent), returns immediately without waiting."
        ),
        {
            "to_agent": {
                "type": "string",
                "description": "Target teammate name (the reviewer).",
            },
            "plan": {
                "type": "string",
                "description": "The plan or proposal to review and approve/reject.",
            },
        },
        ["to_agent", "plan"],
    ),
]
MEMORY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "memory",
        (
            "Read and maintain your persistent cross-session memory: a private "
            "directory of Markdown files plus a MEMORY.md index. Paths are "
            'relative to the memory root (e.g. "MEMORY.md", "user/role.md"). '
            "Sub-agents (explore/plan/code-review) may only use the 'view' command."
        ),
        {
            "command": {
                "type": "string",
                "enum": [
                    "view",
                    "create",
                    "str_replace",
                    "insert",
                    "delete",
                    "rename",
                ],
                "description": "Operation to perform.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Target path for view/create/str_replace/insert/delete. "
                    "Omit (or '.') to view the memory root directory."
                ),
            },
            "file_text": {
                "type": "string",
                "description": "For create: the full file content to write.",
            },
            "old_str": {
                "type": "string",
                "description": "For str_replace: existing text to replace (must be unique).",
            },
            "new_str": {
                "type": "string",
                "description": "For str_replace: replacement text.",
            },
            "insert_line": {
                "type": "integer",
                "description": "For insert: line number to insert after (0 = file start).",
                "minimum": 0,
            },
            "insert_text": {
                "type": "string",
                "description": "For insert: text to insert.",
            },
            "old_path": {
                "type": "string",
                "description": "For rename: existing path.",
            },
            "new_path": {
                "type": "string",
                "description": "For rename: destination path.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "For view: optional [start, end] 1-based inclusive line "
                    "range (end -1 = to end of file)."
                ),
            },
        },
        ["command"],
    ),
]
DEFERRED_TOOL_SCHEMAS: list[dict[str, Any]] = [
    *TODO_TOOL_SCHEMAS,
    *SUBAGENT_TOOL_SCHEMAS,
    *LOAD_SKILL_TOOL_SCHEMAS,
    *TASK_TOOL_SCHEMAS,
    *BACKGROUND_TOOL_SCHEMAS,
    *TEAM_TOOL_SCHEMAS,
    *LSP_TOOL_SCHEMAS,
    SEMANTIC_RENAME_TOOL_SCHEMA,
    *MEMORY_TOOL_SCHEMAS,
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
        (
            "Read a file. UTF-8 text files are returned with line numbers. "
            "Images (.png/.jpg/.jpeg/.gif/.webp) are returned as image blocks "
            "(needs a vision-capable model). PDFs (.pdf) return extracted text "
            'and need the optional [pdf] extra (uv pip install -e ".[pdf]"). '
            "Jupyter notebooks (.ipynb) return rendered markdown/code cells."
        ),
        {
            "file_path": {"type": "string", "description": "Path to the file."},
            "offset": {
                "type": "integer",
                "description": "Zero-based line offset (text files only).",
                "default": 0,
                "minimum": 0,
            },
            "limit": {
                "type": ["integer", "null"],
                "description": "Maximum number of lines to read (text files only).",
                "default": None,
                "minimum": 0,
            },
            "pages": {
                "type": ["string", "null"],
                "description": (
                    "PDF page range, e.g. '1-5' or '3' (1-based). PDF only; omit for all pages."
                ),
                "default": None,
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
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": (
                    "How much to return. 'content' (default): file:line:text per "
                    "match. 'files_with_matches': only the matching file paths — "
                    "the cheapest, use when you just need which files match. "
                    "'count': file:count per file."
                ),
                "default": "content",
            },
        },
        ["pattern"],
    ),
    _schema(
        "web_fetch",
        "Fetch content from a URL. Automatically converts HTML to readable text.",
        {
            "url": {
                "type": "string",
                "description": "URL to fetch (http:// or https://).",
            },
            "max_length": {
                "type": "integer",
                "description": "Maximum characters to return.",
                "default": 10000,
                "minimum": 100,
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds.",
                "default": 15,
                "minimum": 1,
            },
        },
        ["url"],
    ),
    _schema(
        "web_search",
        "Search the web and return structured results.",
        {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return.",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds.",
                "default": 15,
                "minimum": 1,
            },
        },
        ["query"],
    ),
    *DEFERRED_TOOL_SCHEMAS,
]


def _build_diagnostics_hook(
    lsp_manager: LanguageServerManager | None,
) -> Callable[[str, Any], Any] | None:
    """Construct the Hybrid auto-diagnostics callback for file edit/write.

    Returns ``None`` when no manager is wired (LSP disabled). When wired,
    the returned closure is invoked twice by the handler:

    * ``hook(file_path, None)`` — snapshot the current diagnostics so the
      handler can pass them back for diffing. Returns ``list[Diagnostic]``
      or ``None`` if the snapshot is unusable.
    * ``hook(file_path, before)`` — post-edit; returns the formatted
      ``"\\n\\n…"`` appendix or ``None`` (caller appends only on non-None).

    Heavy lifting is delegated to :func:`bareagent.lsp.diagnostics.snapshot_diagnostics`
    and :func:`bareagent.lsp.diagnostics.maybe_diagnostics_appendix`. Both swallow
    their own errors so the handler never fails because of an LSP hiccup.
    """
    if lsp_manager is None:
        return None

    from bareagent.lsp.diagnostics import (
        maybe_diagnostics_appendix,
        snapshot_diagnostics,
    )

    def _hook(file_path: str, before: Any) -> Any:
        # Cheap config gate before touching the manager so disabled mode
        # exits in ~one attribute access.
        try:
            cfg = lsp_manager.config
        except Exception:
            return None
        if not cfg.auto_diagnostics_on_edit:
            return None
        if before is None:
            try:
                return snapshot_diagnostics(lsp_manager, file_path)
            except Exception:
                return None
        return maybe_diagnostics_appendix(lsp_manager, cfg, file_path, before)

    return _hook


def _make_lazy_task_handlers(task_file: Path) -> dict[str, Callable[..., Any]]:
    state: dict[str, dict[str, Callable[..., Any]] | None] = {"handlers": None}

    def _get_handlers() -> dict[str, Callable[..., Any]]:
        handlers = state["handlers"]
        if handlers is None:
            handlers = make_task_handlers(TaskManager(task_file))
            state["handlers"] = handlers
        return handlers

    return {
        "task_create": lambda title, description="", depends_on=None: _get_handlers()[
            "task_create"
        ](
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


_TEAM_FALLBACK_HANDLERS: dict[str, Callable[..., Any]] = {
    "team_spawn": lambda name: f"Team spawning unavailable for {name}.",
    "team_send": lambda to_agent, content: f"Team messaging unavailable for {to_agent}.",
    "team_list": lambda: [],
    "team_shutdown": lambda name: f"Team shutdown unavailable for {name}.",
    "team_register": lambda name, role, system_prompt, provider="", model="": (
        f"Team registration unavailable for {name}."
    ),
    "team_request_review": lambda to_agent, plan: (
        f"Team review unavailable for {to_agent}."
    ),
}


_LSP_UNAVAILABLE_MESSAGE = "Error: LSP manager unavailable."

_LSP_FALLBACK_HANDLERS: dict[str, Callable[..., Any]] = {
    "lsp_outline": lambda **_kw: _LSP_UNAVAILABLE_MESSAGE,
    "lsp_definition": lambda **_kw: _LSP_UNAVAILABLE_MESSAGE,
    "lsp_references": lambda **_kw: _LSP_UNAVAILABLE_MESSAGE,
    "lsp_diagnostics": lambda **_kw: _LSP_UNAVAILABLE_MESSAGE,
    SEMANTIC_RENAME_TOOL_NAME: lambda **_kw: _LSP_UNAVAILABLE_MESSAGE,
}

_MEMORY_DISABLED_MESSAGE = (
    "Error: persistent memory is disabled. Enable it under [memory] in config."
)


def _memory_disabled_handler(**_kw: Any) -> str:
    return _MEMORY_DISABLED_MESSAGE


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "bash": _unbound_stub("bash"),
    "read_file": _unbound_stub("read_file"),
    "write_file": _unbound_stub("write_file"),
    "edit_file": _unbound_stub("edit_file"),
    "glob": _unbound_stub("glob"),
    "grep": _unbound_stub("grep"),
    "web_fetch": run_web_fetch,
    "web_search": run_web_search,
    "todo_read": lambda: _get_default_todo_manager().list(),
    "todo_write": lambda **kw: make_todo_handlers(_get_default_todo_manager())["todo_write"](**kw),
    **_make_lazy_task_handlers(Path(".tasks.json")),
    "load_skill": lambda skill_name: _get_default_skill_loader().load(skill_name),
    "background_run": lambda **_: "Background manager unavailable.",
    "subagent": (
        lambda task, agent_type=None, run_in_background=False: (
            "Subagent unavailable: provider is not configured."
        )
    ),
    **_TEAM_FALLBACK_HANDLERS,
    **_LSP_FALLBACK_HANDLERS,
    "memory": _memory_disabled_handler,
}


def get_tools(
    mcp_manager: MCPManager | None = None,
    lsp_manager: LanguageServerManager | None = None,
) -> list[dict[str, Any]]:
    # LSP tool schemas are already part of TOOL_SCHEMAS (registered via
    # ``DEFERRED_TOOL_SCHEMAS``) so they show up even when no manager is
    # available. ``lsp_manager`` is still required for the handlers — that is
    # bound by ``get_handlers``. We accept the parameter here for forward
    # symmetry with ``mcp_manager`` and so callers can supply both in one go.
    _ = lsp_manager
    schemas = list(TOOL_SCHEMAS)
    if mcp_manager is not None:
        schemas.extend(build_mcp_tool_schemas(mcp_manager))
    return schemas


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
    mcp_manager: MCPManager | None = None,
    lsp_manager: LanguageServerManager | None = None,
    memory_manager: MemoryManager | None = None,
    subagent_retry_policy: Any = None,
    subagent_registry: Any = None,
) -> dict[str, Callable[..., Any]]:
    # Hybrid auto-diagnostics hook: built once per ``get_handlers`` call so
    # edit_file / write_file share the same closure. ``None`` when LSP isn't
    # wired in — handlers will then skip the snapshot/diff entirely. Importing
    # the hook here (rather than in the handler modules) keeps src/core/
    # free of any direct dependency on src/lsp/.
    diagnostics_hook = _build_diagnostics_hook(lsp_manager)

    handlers: dict[str, Callable[..., Any]] = {
        "bash": partial(run_bash, cwd=workspace),
        "read_file": partial(run_read, workspace=workspace),
        "write_file": partial(run_write, workspace=workspace, diagnostics_hook=diagnostics_hook),
        "edit_file": partial(run_edit, workspace=workspace, diagnostics_hook=diagnostics_hook),
        "glob": partial(run_glob, workspace=workspace),
        "grep": partial(run_grep, workspace=workspace),
        "web_fetch": run_web_fetch,
        "web_search": run_web_search,
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

    handlers.update(team_handlers or _TEAM_FALLBACK_HANDLERS)

    if mcp_manager is not None:
        handlers.update(build_mcp_handlers(mcp_manager))

    if lsp_manager is not None:
        _, lsp_handlers = build_lsp_tools(lsp_manager)
        handlers.update(lsp_handlers)
    else:
        handlers.update(_LSP_FALLBACK_HANDLERS)

    if memory_manager is not None:
        handlers["memory"] = partial(run_memory, manager=memory_manager)
    else:
        handlers["memory"] = _memory_disabled_handler

    available_tools = tools or get_tools(mcp_manager, lsp_manager)
    if provider is None:
        handlers["subagent"] = (
            lambda task, agent_type=None, run_in_background=False, isolation="none": (
                "Subagent unavailable: provider is not configured."
            )
        )
    else:
        handlers["subagent"] = (
            lambda task, agent_type=None, run_in_background=False, isolation="none": run_subagent(
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
                isolation=isolation,
                retry_policy=subagent_retry_policy,
                # Only top-level (main-loop) subagents register a resumable
                # context; nested spawns pass registry=None inside run_subagent.
                registry=subagent_registry,
            )
        )

    return handlers


def rebind_workspace_handlers(
    handlers: dict[str, Callable[..., Any]],
    new_workspace: Path,
) -> dict[str, Callable[..., Any]]:
    """Return a shallow copy of *handlers* with file ops rooted at *new_workspace*.

    Only the six workspace-bound handlers (bash/read/write/edit/glob/grep) are
    replaced; every other handler (todo/task/skill/memory/mcp/lsp/subagent/
    web_*/background_run) keeps its parent binding. ``bash`` rebinds its ``cwd``
    keyword, the rest rebind ``workspace``. ``write_file`` / ``edit_file`` carry
    a ``diagnostics_hook`` keyword on their original partial that must be
    preserved across the rebind, so it is read back from ``.keywords``.
    """
    rebound = dict(handlers)

    diag_hook = _extract_diagnostics_hook(handlers.get("write_file"))
    if diag_hook is None:
        diag_hook = _extract_diagnostics_hook(handlers.get("edit_file"))

    rebound["bash"] = partial(run_bash, cwd=new_workspace)
    rebound["read_file"] = partial(run_read, workspace=new_workspace)
    rebound["write_file"] = partial(run_write, workspace=new_workspace, diagnostics_hook=diag_hook)
    rebound["edit_file"] = partial(run_edit, workspace=new_workspace, diagnostics_hook=diag_hook)
    rebound["glob"] = partial(run_glob, workspace=new_workspace)
    rebound["grep"] = partial(run_grep, workspace=new_workspace)
    return rebound


def _extract_diagnostics_hook(handler: Any) -> Any:
    """Read a ``diagnostics_hook`` keyword off a partial, or ``None``."""
    keywords = getattr(handler, "keywords", None)
    if isinstance(keywords, dict):
        return keywords.get("diagnostics_hook")
    return None


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
