from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import tomllib
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from src.concurrency.background import BackgroundManager
from src.core.context import assemble_system_prompt
from src.core.fileutil import (
    generate_random_id,
    is_tool_result_message,
)
from src.core.fileutil import (
    optional_string as _coerce_optional_string,
)
from src.core.loop import LLMCallError, agent_loop
from src.core.tools import get_handlers, get_tools
from src.debug.interaction_log import InteractionLogger
from src.lsp import (
    LanguageServerManager,
    LSPConfig,
    LSPError,
    parse_lsp_config,
)
from src.mcp import MCPCallError, MCPConfig, MCPError, MCPManager, parse_mcp_config
from src.mcp.registry import _flatten_content as _mcp_flatten_content
from src.memory.compact import Compactor
from src.memory.persistent import (
    MemoryManager,
    build_forget_instruction,
    build_remember_instruction,
    resolve_memory_root,
)
from src.memory.transcript import TranscriptManager
from src.permission.guard import (
    PermissionGuard,
    PermissionMode,
    permission_rule_subject,
)
from src.permission.rules import parse_permission_rules
from src.planning.agent_types import BUILTIN_AGENT_TYPES, DEFAULT_AGENT_TYPE
from src.planning.skills import SkillLoader, resolve_skills_dir
from src.planning.tasks import TaskManager
from src.planning.todo import TodoManager
from src.provider.base import VALID_THINKING_MODES, BaseLLMProvider, ThinkingConfig
from src.provider.factory import create_provider
from src.team.autonomous import AutonomousAgent
from src.team.mailbox import Message, MessageBus
from src.team.manager import TeammateManager
from src.team.protocols import Protocol, ProtocolFSM, decode_protocol_content
from src.ui.console import AgentConsole

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
VALID_PERMISSION_MODES = {m.value for m in PermissionMode}
VALID_SUBAGENT_TYPES = set(BUILTIN_AGENT_TYPES)
MAIN_AGENT_NAME = "main"
DEFAULT_API_KEY_ENV_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_SESSION_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S-%f"


@dataclass(slots=True)
class ProviderConfig:
    name: str
    model: str
    api_key_env: str
    base_url: str | None = None
    wire_api: str | None = None


@dataclass(slots=True)
class PermissionConfig:
    mode: str
    allow: list[str]
    deny: list[str]


@dataclass(slots=True)
class UIConfig:
    stream: bool
    theme: str


@dataclass(slots=True)
class SubagentConfig:
    max_depth: int
    default_type: str


@dataclass(slots=True)
class DebugConfig:
    enabled: bool = False
    log_dir: str = ".logs"
    viewer_port: int = 8321
    pretty: bool = True


@dataclass(slots=True)
class TracingConfig:
    langfuse: bool = False
    opentelemetry: bool = False
    content_enabled: bool = True


@dataclass(slots=True)
class MemoryConfig:
    enabled: bool = True
    # Memory root. Empty -> per-project default under ~/.bareagent/projects/.
    dir: str = ""
    # Max lines of MEMORY.md injected into the system prompt at session start.
    max_index_lines: int = 200
    # Number of lexically-relevant memories recalled and injected each turn
    # (0 = disable recall, keeping only the session-start index injection).
    recall_k: int = 5


@dataclass(slots=True)
class Config:
    provider: ProviderConfig
    permission: PermissionConfig
    ui: UIConfig
    subagent: SubagentConfig
    thinking: ThinkingConfig
    debug: DebugConfig
    tracing: TracingConfig
    path: Path
    mcp: MCPConfig
    lsp: LSPConfig
    # Defaulted so existing Config(...) constructions (tests, fixtures) keep
    # working without passing memory explicitly.
    memory: MemoryConfig = field(default_factory=MemoryConfig)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bareagent")
    parser.add_argument("--provider", help="Override the configured provider name.")
    parser.add_argument("--model", help="Override the configured model name.")
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Path to the TOML config file. Defaults to BAREAGENT_CONFIG or the bundled config.toml."
        ),
    )
    return parser.parse_args(argv)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (returns a new dict)."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_config_file(config_path: Path) -> dict:
    with config_path.open("rb") as file:
        base = tomllib.load(file)
    local_path = config_path.with_suffix("").with_name(
        config_path.stem + ".local" + config_path.suffix,
    )
    if local_path.is_file():
        with local_path.open("rb") as file:
            local = tomllib.load(file)
        return _deep_merge(base, local)
    return base


def _resolve_string(
    file_value: str,
    env_name: str,
    cli_value: str | None = None,
) -> str:
    if cli_value is not None:
        return cli_value
    return os.getenv(env_name, file_value)


def _resolve_bool(file_value: bool, env_name: str) -> bool:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return file_value

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} must be a boolean value, got: {raw_value}")


def _resolve_int(file_value: int, env_name: str) -> int:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return file_value
    return int(raw_value)


def _resolve_optional_string(file_value: str | None, env_name: str) -> str | None:
    raw_value = os.getenv(env_name)
    value = raw_value if raw_value is not None else file_value
    if value in {None, ""}:
        return None
    return value


def _validate_mode(name: str, value: str, allowed: AbstractSet[str]) -> str:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_values}")
    return value


def _default_api_key_env(provider_name: str) -> str:
    return DEFAULT_API_KEY_ENV_BY_PROVIDER.get(provider_name.lower(), "ANTHROPIC_API_KEY")


def resolve_config_path(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser()

    env_path = os.getenv("BAREAGENT_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    return DEFAULT_CONFIG_PATH


def load_config(
    config_path: Path,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> Config:
    raw_config = _read_config_file(config_path)
    provider_raw = raw_config.get("provider", {})
    permission_raw = raw_config.get("permission", {})
    ui_raw = raw_config.get("ui", {})
    subagent_raw = raw_config.get("subagent", {})
    thinking_raw = raw_config.get("thinking", {})
    debug_raw = raw_config.get("debug", {})
    tracing_raw = raw_config.get("tracing", {})
    allow_rules, deny_rules = parse_permission_rules(raw_config)
    configured_provider_name = str(provider_raw.get("name", "anthropic"))
    provider_name = _resolve_string(
        configured_provider_name,
        "BAREAGENT_PROVIDER",
        provider_override,
    )
    default_api_key_env = _default_api_key_env(provider_name)
    configured_api_key_env = provider_raw.get("api_key_env")
    api_key_env_default = (
        configured_api_key_env
        if configured_api_key_env and provider_name == configured_provider_name
        else default_api_key_env
    )

    provider = ProviderConfig(
        name=provider_name,
        model=_resolve_string(
            provider_raw.get("model", "claude-sonnet-4-20250514"),
            "BAREAGENT_MODEL",
            model_override,
        ),
        api_key_env=_resolve_string(
            api_key_env_default,
            "BAREAGENT_API_KEY_ENV",
        ),
        base_url=_resolve_optional_string(
            provider_raw.get("base_url"),
            "BAREAGENT_BASE_URL",
        ),
        wire_api=_resolve_optional_string(
            provider_raw.get("wire_api"),
            "BAREAGENT_WIRE_API",
        ),
    )
    permission = PermissionConfig(
        mode=_validate_mode(
            "permission.mode",
            _resolve_string(
                permission_raw.get("mode", "default"),
                "BAREAGENT_PERMISSION_MODE",
            ),
            VALID_PERMISSION_MODES,
        ),
        allow=allow_rules,
        deny=deny_rules,
    )
    ui = UIConfig(
        stream=_resolve_bool(ui_raw.get("stream", True), "BAREAGENT_UI_STREAM"),
        theme=_resolve_string(ui_raw.get("theme", "dark"), "BAREAGENT_UI_THEME"),
    )
    subagent = SubagentConfig(
        max_depth=_resolve_int(
            int(subagent_raw.get("max_depth", 3)),
            "BAREAGENT_SUBAGENT_MAX_DEPTH",
        ),
        default_type=_validate_mode(
            "subagent.default_type",
            _resolve_string(
                str(subagent_raw.get("default_type", DEFAULT_AGENT_TYPE)),
                "BAREAGENT_SUBAGENT_DEFAULT_TYPE",
            ),
            VALID_SUBAGENT_TYPES,
        ),
    )
    thinking = ThinkingConfig(
        mode=cast(
            Literal["enabled", "adaptive", "disabled"],
            _validate_mode(
                "thinking.mode",
                _resolve_string(
                    thinking_raw.get("mode", "adaptive"),
                    "BAREAGENT_THINKING_MODE",
                ),
                VALID_THINKING_MODES,
            ),
        ),
        budget_tokens=_resolve_int(
            int(thinking_raw.get("budget_tokens", 10000)),
            "BAREAGENT_THINKING_BUDGET_TOKENS",
        ),
    )
    debug = DebugConfig(
        enabled=_resolve_bool(
            bool(debug_raw.get("enabled", False)),
            "BAREAGENT_DEBUG",
        ),
        log_dir=_resolve_string(
            str(debug_raw.get("log_dir", ".logs")),
            "BAREAGENT_DEBUG_LOG_DIR",
        ),
        viewer_port=_resolve_int(
            int(debug_raw.get("viewer_port", 8321)),
            "BAREAGENT_DEBUG_VIEWER_PORT",
        ),
        pretty=_resolve_bool(
            bool(debug_raw.get("pretty", True)),
            "BAREAGENT_DEBUG_PRETTY",
        ),
    )
    tracing = TracingConfig(
        langfuse=_resolve_bool(
            bool(tracing_raw.get("langfuse", False)),
            "BAREAGENT_TRACING_LANGFUSE",
        ),
        opentelemetry=_resolve_bool(
            bool(tracing_raw.get("opentelemetry", False)),
            "BAREAGENT_TRACING_OPENTELEMETRY",
        ),
        content_enabled=_resolve_bool(
            bool(tracing_raw.get("content_enabled", True)),
            "BAREAGENT_CONTENT_TRACING_ENABLED",
        ),
    )

    mcp_raw = raw_config.get("mcp", {})
    try:
        mcp_config = parse_mcp_config({"mcp": mcp_raw} if isinstance(mcp_raw, dict) else {})
    except MCPError as exc:
        print(f"Warning: invalid [mcp] config, MCP disabled ({exc})")
        mcp_config = MCPConfig()

    lsp_raw = raw_config.get("lsp", {})
    try:
        lsp_config = parse_lsp_config({"lsp": lsp_raw} if isinstance(lsp_raw, dict) else {})
    except LSPError as exc:
        print(f"Warning: invalid [lsp] config, LSP disabled ({exc})")
        lsp_config = LSPConfig()

    memory_raw = raw_config.get("memory", {})
    memory_config = MemoryConfig(
        enabled=_resolve_bool(
            bool(memory_raw.get("enabled", True)),
            "BAREAGENT_MEMORY_ENABLED",
        ),
        dir=_resolve_string(
            str(memory_raw.get("dir", "")),
            "BAREAGENT_MEMORY_DIR",
        ),
        max_index_lines=_resolve_int(
            int(memory_raw.get("max_index_lines", 200)),
            "BAREAGENT_MEMORY_MAX_INDEX_LINES",
        ),
        recall_k=_resolve_int(
            int(memory_raw.get("recall_k", 5)),
            "BAREAGENT_MEMORY_RECALL_K",
        ),
    )

    return Config(
        provider=provider,
        permission=permission,
        ui=ui,
        subagent=subagent,
        thinking=thinking,
        debug=debug,
        tracing=tracing,
        path=config_path.resolve(),
        mcp=mcp_config,
        lsp=lsp_config,
        memory=memory_config,
    )


_NAG_REMINDER_PREFIX = "<nag-reminder>"
_MEMORY_RECALL_PREFIX = "<memory-recall>"


def _initial_messages(
    workspace: Path,
    skill_summary: str = "",
    memory_context: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": assemble_system_prompt(
                workspace,
                skill_summary=skill_summary,
                memory_context=memory_context,
            ),
        }
    ]


def _build_memory_manager(
    config: Config,
    workspace_path: Path,
    ui_console: AgentConsole,
) -> MemoryManager | None:
    """Build the persistent memory manager, or None when disabled/unavailable."""
    if not config.memory.enabled:
        return None
    try:
        root = resolve_memory_root(workspace_path, config.memory.dir)
        return MemoryManager(root, max_index_lines=config.memory.max_index_lines)
    except OSError as exc:
        ui_console.print_error(f"Persistent memory disabled (cannot open store): {exc}")
        return None


def _memory_context(memory_manager: MemoryManager | None) -> str:
    return memory_manager.system_prompt_section() if memory_manager is not None else ""


def _refresh_nag_reminder(
    messages: list[dict[str, Any]],
    nag_reminder: str | None,
) -> None:
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and str(message["content"]).startswith(_NAG_REMINDER_PREFIX)
        )
    ]
    if not nag_reminder:
        return

    nag_message = {
        "role": "system",
        "content": f"{_NAG_REMINDER_PREFIX}\n{nag_reminder.strip()}\n</nag-reminder>",
    }
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and not is_tool_result_message(msg):
            messages.insert(index + 1, nag_message)
            return

    messages.append(nag_message)


def _refresh_memory_recall(
    messages: list[dict[str, Any]],
    memory_manager: MemoryManager | None,
    recall_k: int,
) -> None:
    """Drop the stale recall block and inject one for the latest user turn.

    Mirrors :func:`_refresh_nag_reminder`: a single ``<memory-recall>`` system
    message lives just after the most recent genuine user message, refreshed on
    every agent-loop iteration so ``/remember``, ``/forget`` and ordinary turns
    all pick up the latest lexically-relevant memories.
    """
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and str(message["content"]).startswith(_MEMORY_RECALL_PREFIX)
        )
    ]
    if memory_manager is None or recall_k <= 0:
        return

    query: str | None = None
    insert_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and not is_tool_result_message(msg):
            content = msg.get("content")
            if isinstance(content, str):
                query = content
                insert_index = index
            break
    if query is None or insert_index is None:
        return

    section = memory_manager.recall_section(query, recall_k)
    if not section:
        return

    messages.insert(insert_index + 1, {"role": "system", "content": section})


def _build_loop_compact(
    compact_fn: object,
    todo_manager: TodoManager,
    memory_manager: MemoryManager | None = None,
    recall_k: int = 0,
):
    def _compact(
        messages: list[dict[str, Any]],
        force: bool = False,
    ) -> None:
        _refresh_nag_reminder(messages, todo_manager.get_nag_reminder())
        _refresh_memory_recall(messages, memory_manager, recall_k)
        compact_fn(messages, force=force)  # type: ignore[misc]

    get_session_id = getattr(compact_fn, "get_session_id", None)
    if callable(get_session_id):
        _compact.get_session_id = get_session_id  # type: ignore[attr-defined]

    set_session_id = getattr(compact_fn, "set_session_id", None)
    if callable(set_session_id):
        _compact.set_session_id = set_session_id  # type: ignore[attr-defined]

    return _compact


_PERMISSION_SLASH = {
    "/default": PermissionMode.DEFAULT,
    "/auto": PermissionMode.AUTO,
    "/plan": PermissionMode.PLAN,
    "/bypass": PermissionMode.BYPASS,
}
_MODE_CYCLE = [
    PermissionMode.DEFAULT,
    PermissionMode.AUTO,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]
_MODE_DESCRIPTIONS = {
    PermissionMode.DEFAULT: "Write operations require confirmation",
    PermissionMode.AUTO: "Safe commands auto-approved",
    PermissionMode.PLAN: "Read-only mode",
    PermissionMode.BYPASS: "No confirmation required",
}
_SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/clear",
    "/new",
    "/compact",
    *_PERMISSION_SLASH,
    "/mode",
    "/theme",
    "/sessions",
    "/resume",
    "/log",
    "/team",
    "/mcp",
    "/mcp:",
    "/lsp",
    "/remember",
    "/forget",
]
_HELP_TEXT = (
    "Available commands:\n"
    "  /help      Show this help message\n"
    "  /exit      Exit BareAgent\n"
    "  /clear     Clear screen and start new conversation\n"
    "  /new       Start a new conversation\n"
    "  /compact   Compress conversation context\n"
    "  /default   Switch to DEFAULT permission mode\n"
    "  /auto      Switch to AUTO permission mode\n"
    "  /plan      Switch to PLAN permission mode\n"
    "  /bypass    Switch to BYPASS permission mode\n"
    "  /mode      Interactive permission mode selection\n"
    "  /theme     Switch color theme (catppuccin-mocha, dracula, nord, tokyo-night, gruvbox)\n"
    "  /sessions  List saved sessions\n"
    "  /resume    Resume a previous session\n"
    "  /log       Debug log viewer (status|serve|open|<seq>)\n"
    "  /team      Manage team agents (list | spawn | send)\n"
    "  /mcp       Manage MCP servers (status | list | reload <name>)\n"
    "  /mcp:      Invoke an MCP prompt (e.g. /mcp:server:prompt key=value)\n"
    "  /lsp       Manage LSP servers (status | list | reload <language>)\n"
    "  /remember  Save information to persistent memory (/remember <text>)\n"
    "  /forget    Remove information from persistent memory (/forget <text>)"
)


def _build_permission_guard(config: Config) -> PermissionGuard:
    guard = PermissionGuard(PermissionMode(config.permission.mode))
    guard.allow_rules = list(config.permission.allow)
    guard.deny_rules = list(config.permission.deny)
    return guard


def _build_permission_allow_rule(
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    normalized_tool = tool_name.strip().lower()
    subject = permission_rule_subject(normalized_tool, tool_input)
    if not subject:
        return None
    if "\n" in subject or "\r" in subject:
        encoded_subject = json.dumps(subject, ensure_ascii=False)
        return f"{normalized_tool}(prefix_json:{encoded_subject})"
    return f"{normalized_tool}(prefix:{subject}*)"


def _persist_permission_allow_rule(
    permission: PermissionGuard,
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    rule = _build_permission_allow_rule(tool_name, tool_input)
    if rule is None or rule in permission.allow_rules:
        return
    permission.allow_rules.append(rule)


def _install_stdio_permission_prompt(
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    if not sys.stdin.isatty():
        return

    def _ask(call: Any) -> bool:
        preview_input = _build_permission_ask_payload(permission, call.name, call.input)
        allowed = ui_console.ask_permission(call.name, preview_input)
        choice = ui_console.consume_permission_choice()
        if allowed and choice == "always":
            _persist_permission_allow_rule(permission, call.name, call.input)
        return allowed

    permission._ask_user_fn = _ask


def _build_permission_ask_payload(
    permission: PermissionGuard,
    tool_name: str,
    tool_input: Any,
) -> Any:
    """Truncate oversized top-level string fields when asking about an MCP tool.

    Non-MCP tools fall back to the raw input dict so existing rendering and
    permission rules stay unchanged. For MCP tools the guard's
    ``format_preview`` rule (256 chars per top-level string) is applied by
    rebuilding the dict — the console layer keeps doing the JSON pretty-print.
    """
    if not isinstance(tool_input, dict):
        return tool_input
    normalized = tool_name.strip().lower()
    if not normalized.startswith("mcp__"):
        return tool_input
    # Reuse the guard's truncation rule by parsing the formatted JSON back into
    # a dict — keeps the truncation logic in one place.
    try:
        truncated = json.loads(permission.format_preview(tool_name, tool_input))
    except (TypeError, ValueError):
        return tool_input
    if not isinstance(truncated, dict):
        return tool_input
    return truncated


def _generate_session_id(
    transcript_mgr: TranscriptManager,
    *,
    reserved_ids: set[str] | None = None,
) -> str:
    known_session_ids = set(transcript_mgr.list_sessions())
    if reserved_ids:
        known_session_ids.update(session_id for session_id in reserved_ids if session_id)

    while True:
        candidate = (
            f"{datetime.now().strftime(_SESSION_ID_TIMESTAMP_FORMAT)}-{generate_random_id(6)}"
        )
        if candidate not in known_session_ids:
            return candidate


def _switch_session_mailbox(
    workspace_path: Path,
    session_id: str,
    *,
    current_bus: MessageBus | None = None,
) -> tuple[MessageBus, str | None]:
    if current_bus is not None:
        _broadcast_team_shutdown(current_bus)

    message_bus = MessageBus(workspace_path / ".mailbox" / session_id)
    message_bus.ensure_mailbox(MAIN_AGENT_NAME)
    return message_bus, message_bus.latest_message_id(MAIN_AGENT_NAME)


def _get_compact_session_id(compact_fn: object) -> str:
    getter = getattr(compact_fn, "get_session_id", None)
    if callable(getter):
        return str(getter())
    return "default"


def _set_compact_session_id(compact_fn: object, session_id: str) -> None:
    setter = getattr(compact_fn, "set_session_id", None)
    if callable(setter):
        setter(session_id)


def _save_transcript_snapshot(
    transcript_mgr: TranscriptManager,
    messages: list[dict[str, Any]],
    compact_fn: object,
) -> None:
    transcript_mgr.save(messages, _get_compact_session_id(compact_fn))


def _resolve_debug_log_dir(workspace_path: Path, config: Config) -> Path:
    log_dir = Path(config.debug.log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = workspace_path / log_dir
    return log_dir


def _build_interaction_logger(
    config: Config,
    workspace_path: Path,
    session_id: str,
) -> InteractionLogger | None:
    if not config.debug.enabled:
        return None

    return InteractionLogger(
        log_dir=_resolve_debug_log_dir(workspace_path, config),
        session_id=session_id,
        pretty=config.debug.pretty,
    )


def _set_interaction_logger_session(
    interaction_logger: InteractionLogger | None,
    session_id: str,
) -> None:
    if interaction_logger is None:
        return
    interaction_logger.session_id = session_id


def _configure_tracing(
    config: Config,
    *,
    session_id: str = "default",
    interaction_logger: InteractionLogger | None = None,
) -> None:
    from src.tracing.setup import configure_tracing

    configure_tracing(
        config.tracing,
        session_id=session_id,
        interaction_logger=interaction_logger,
    )


def _debug_viewer_url(config: Config) -> str:
    return f"http://127.0.0.1:{config.debug.viewer_port}"


def _format_log_status(
    config: Config,
    interaction_logger: InteractionLogger,
    viewer_server: object | None,
) -> str:
    interactions = interaction_logger.list_interactions(interaction_logger.session_id)
    total_tokens = sum(
        int(interaction.get("input_tokens", 0) or 0) + int(interaction.get("output_tokens", 0) or 0)
        for interaction in interactions
    )
    lines = [
        "Debug logging: enabled",
        f"Log dir: {config.debug.log_dir}",
        f"Current session: {interaction_logger.session_id}",
        f"Interactions: {len(interactions)}",
        f"Total tokens: {total_tokens}",
        f"Sessions: {len(interaction_logger.list_sessions())}",
    ]
    if viewer_server is None:
        lines.append("Viewer: not running (use /log serve)")
    else:
        lines.append(f"Viewer: {_debug_viewer_url(config)}")
    return "\n".join(lines)


def _format_log_interaction_summary(
    seq: int,
    interaction: dict[str, object],
) -> str:
    request = interaction.get("request")
    response = interaction.get("response")
    if not request and not response:
        return f"Interaction #{seq} not found."

    response_data = response if isinstance(response, dict) else {}
    tool_calls = response_data.get("tool_calls", [])
    thinking = str(response_data.get("thinking", "") or "").strip()
    lines = [
        f"Interaction #{seq}:",
        f"  Input tokens:  {response_data.get('input_tokens', '?')}",
        f"  Output tokens: {response_data.get('output_tokens', '?')}",
        f"  Duration:      {response_data.get('duration_ms', '?')}ms",
        f"  Tool calls:    {len(tool_calls) if isinstance(tool_calls, list) else 0}",
    ]
    if response_data.get("error"):
        lines.append(f"  Error: {response_data['error']}")
    if thinking:
        preview = thinking[:100]
        if len(thinking) > 100:
            preview += "..."
        lines.append(f"  Thinking: {preview}")
    return "\n".join(lines)


def _start_debug_viewer(
    interaction_logger: InteractionLogger,
    config: Config,
) -> object:
    from src.debug.web_viewer import start_viewer

    viewer_server, _ = start_viewer(
        interaction_logger,
        port=config.debug.viewer_port,
    )
    return viewer_server


def _handle_log_command(
    text: str,
    *,
    config: Config,
    interaction_logger: InteractionLogger | None,
    viewer_server: object | None,
    print_status: Callable[[str], None],
) -> object | None:
    _, _, log_arg = text.partition(" ")
    log_cmd = log_arg.strip()

    if interaction_logger is None:
        print_status(
            "Debug logging is disabled. Set [debug] enabled = true in config.toml "
            "or BAREAGENT_DEBUG=1"
        )
        return viewer_server

    if not log_cmd or log_cmd == "status":
        print_status(_format_log_status(config, interaction_logger, viewer_server))
        return viewer_server

    if log_cmd in {"serve", "open"}:
        if viewer_server is None:
            try:
                viewer_server = _start_debug_viewer(interaction_logger, config)
            except OSError as exc:
                print_status(f"Failed to start debug viewer: {exc}")
                return viewer_server
            print_status(f"Debug viewer started at {_debug_viewer_url(config)}")
        elif log_cmd == "serve":
            print_status(f"Viewer already running at {_debug_viewer_url(config)}")

        if log_cmd == "open":
            import webbrowser

            url = _debug_viewer_url(config)
            webbrowser.open(url)
            print_status(f"Opening {url} in browser...")
        return viewer_server

    try:
        seq = int(log_cmd)
    except ValueError:
        print_status("Usage: /log [status|serve|open|<seq>]")
        return viewer_server

    interaction = interaction_logger.get_interaction(
        interaction_logger.session_id,
        seq,
    )
    print_status(_format_log_interaction_summary(seq, interaction))
    return viewer_server


def _build_handlers(
    *,
    workspace_path: Path,
    todo_manager: TodoManager,
    task_manager: TaskManager | None,
    skill_loader: SkillLoader,
    provider: BaseLLMProvider,
    tools: list[dict[str, object]],
    permission: PermissionGuard,
    bg_manager: BackgroundManager,
    messages: list[dict[str, Any]],
    config: Config,
    runtime_id: str,
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
    mcp_manager: MCPManager | None = None,
    lsp_manager: LanguageServerManager | None = None,
    memory_manager: MemoryManager | None = None,
    system_prompt_override: str | None = None,
) -> dict[str, Callable[..., Any]]:
    system_prompt = system_prompt_override or _extract_system_prompt(messages)
    team_handlers = _make_team_handlers(
        config=config,
        workspace_path=workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        permission=permission,
        bg_manager=bg_manager,
        tools=tools,
        runtime_id=runtime_id,
        teammate_manager=teammate_manager,
        message_bus=message_bus,
        spawned_agents=spawned_agents,
        agent_name=agent_name,
    )
    return get_handlers(
        workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        provider=provider,
        tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        subagent_system_prompt=system_prompt,
        subagent_max_depth=config.subagent.max_depth,
        subagent_default_type=config.subagent.default_type,
        team_handlers=team_handlers,
        mcp_manager=mcp_manager,
        lsp_manager=lsp_manager,
        memory_manager=memory_manager,
    )


def _load_task_manager(
    workspace_path: Path,
    agent_console: AgentConsole,
) -> TaskManager | None:
    try:
        return TaskManager(workspace_path / ".tasks.json")
    except Exception as exc:
        agent_console.print_error(
            f"Failed to load task file {workspace_path / '.tasks.json'}: {exc}"
        )
        return None


def _load_teammate_manager(
    workspace_path: Path,
    agent_console: AgentConsole,
) -> TeammateManager:
    team_file = workspace_path / ".team.json"
    try:
        return TeammateManager(team_file)
    except Exception as exc:
        agent_console.print_error(f"Failed to load team file {team_file}: {exc}")
        return TeammateManager.create_empty(team_file)


def _extract_system_prompt(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _make_team_handlers(
    *,
    config: Config,
    workspace_path: Path,
    todo_manager: TodoManager,
    task_manager: TaskManager | None,
    skill_loader: SkillLoader,
    permission: PermissionGuard,
    bg_manager: BackgroundManager,
    tools: list[dict[str, object]],
    runtime_id: str,
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
) -> dict[str, Callable[..., Any]]:
    provider_factory = _make_teammate_provider_factory(config)

    def _team_list() -> list[dict[str, object]]:
        return [
            {
                **teammate.to_dict(),
                "running": teammate.name in spawned_agents,
            }
            for teammate in teammate_manager.list()
        ]

    def _team_send(to_agent: str, content: str) -> str:
        normalized_target = to_agent.strip()
        if normalized_target != MAIN_AGENT_NAME:
            teammate_manager.get(normalized_target)
        message_id = message_bus.send(
            Message(
                id="",
                from_agent=agent_name,
                to_agent=normalized_target,
                content=content,
                msg_type="request",
                timestamp="",
            )
        )
        return f"Sent message {message_id} to {normalized_target}"

    def _team_spawn(name: str) -> str:
        teammate_name = name.strip()
        agent_instance = teammate_manager.spawn(teammate_name, provider_factory)
        message_bus.ensure_mailbox(teammate_name)
        teammate_permission = permission.clone(fail_closed=True)
        agent_handlers = _build_handlers(
            workspace_path=workspace_path,
            todo_manager=todo_manager,
            task_manager=task_manager,
            skill_loader=skill_loader,
            provider=agent_instance.provider,
            tools=tools,
            permission=teammate_permission,
            bg_manager=bg_manager,
            messages=[],
            config=config,
            runtime_id=runtime_id,
            teammate_manager=teammate_manager,
            message_bus=message_bus,
            spawned_agents=spawned_agents,
            agent_name=teammate_name,
            system_prompt_override=agent_instance.system_prompt,
        )
        autonomous_agent = AutonomousAgent(
            name=agent_instance.name,
            provider=agent_instance.provider,
            tools=tools,
            handlers=agent_handlers,
            bus=message_bus,
            task_manager=task_manager,
            permission=teammate_permission,
            system_prompt=agent_instance.system_prompt,
            poll_interval=1.0,
        )
        try:
            bg_manager.submit(f"team:{runtime_id}:{teammate_name}", autonomous_agent.run)
        except ValueError:
            return f"Teammate {teammate_name} is already running."
        spawned_agents[teammate_name] = autonomous_agent
        return f"Spawned teammate {teammate_name} ({agent_instance.role})"

    return {
        "team_list": _team_list,
        "team_send": _team_send,
        "team_spawn": _team_spawn,
    }


def _make_teammate_provider_factory(config: Config):
    def _factory(provider_overrides: dict[str, object]) -> BaseLLMProvider:
        provider_name = str(provider_overrides.get("name", config.provider.name)).strip()
        resolved_provider_name = provider_name or config.provider.name
        inherited_api_key_env = (
            config.provider.api_key_env
            if resolved_provider_name == config.provider.name
            else _default_api_key_env(resolved_provider_name)
        )
        api_key_env = (
            str(
                provider_overrides.get(
                    "api_key_env",
                    inherited_api_key_env,
                )
            ).strip()
            or inherited_api_key_env
        )
        provider_config = ProviderConfig(
            name=resolved_provider_name,
            model=str(provider_overrides.get("model", config.provider.model)).strip()
            or config.provider.model,
            api_key_env=api_key_env,
            base_url=_coerce_optional_string(
                provider_overrides.get("base_url", config.provider.base_url)
            ),
            wire_api=_coerce_optional_string(
                provider_overrides.get("wire_api", config.provider.wire_api)
            ),
        )
        return create_provider(
            SimpleNamespace(
                provider=provider_config,
                thinking=ThinkingConfig(
                    mode=config.thinking.mode,
                    budget_tokens=config.thinking.budget_tokens,
                ),
            )
        )

    return _factory


def _handle_team_command(
    text: str,
    ui_console: AgentConsole,
    *,
    teammate_manager: TeammateManager,
    team_handlers: dict[str, Callable[..., Any]],
) -> None:
    _, _, raw_args = text.partition(" ")
    parts = raw_args.split(" ", 2) if raw_args else []
    subcommand = parts[0] if parts else "list"

    try:
        if subcommand == "list":
            teammates = team_handlers["team_list"]()  # type: ignore[index, operator]
            if not teammates:
                ui_console.print_status("No teammates registered.")
                return
            for teammate in teammates:
                name = str(teammate.get("name", "unknown"))
                role = str(teammate.get("role", ""))
                running = "running" if teammate.get("running") else "idle"
                ui_console.console.print(f"{name} [{running}] - {role}")
            return

        if subcommand == "spawn" and len(parts) >= 2:
            result = team_handlers["team_spawn"](parts[1])  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return

        if subcommand == "send" and len(parts) >= 3:
            target = parts[1].strip()
            content = parts[2].strip()
            if not target or not content:
                raise ValueError("Usage: /team send <name> <message>")
            result = team_handlers["team_send"](target, content)  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return
    except Exception as exc:
        ui_console.print_error(str(exc))
        return

    ui_console.print_status("Usage: /team list | /team spawn <name> | /team send <name> <message>")


_MCP_PROMPT_USAGE = "Usage: /mcp:<server>:<prompt> [key=value ...]"
_MCP_COMMAND_USAGE = "Usage: /mcp <status|list|reload <name>>"


def _dispatch_mcp_command(
    text: str,
    *,
    mcp_manager: MCPManager,
    ui_console: AgentConsole,
) -> None:
    """Handle the space-prefixed ``/mcp <subcommand>`` REPL command.

    Returns nothing — feedback goes through ``ui_console``. Unknown
    subcommands or missing args become ``print_error`` lines and never raise.
    """
    tokens = text.split()
    if len(tokens) <= 1:
        ui_console.print_status(_MCP_COMMAND_USAGE)
        return
    sub = tokens[1]
    if sub == "status":
        rows = mcp_manager.summarize()
        if not rows:
            ui_console.print_status("(no MCP servers configured)")
            return
        for row in rows:
            resources_label = "resources" if row["has_resources"] else "no-resources"
            ui_console.print_status(
                f"{row['name']}: {row['status']} "
                f"[{row['tool_count']} tools, "
                f"{resources_label}, "
                f"{row['prompt_count']} prompts]"
            )
        return
    if sub == "list":
        any_server = False
        for name, client in mcp_manager.iter_running_clients():
            any_server = True
            ui_console.print_status(f"[{name}]")
            cached_tools = getattr(client, "_tools_cache", None) or []
            for tool in cached_tools:
                tool_name = tool.get("name") if isinstance(tool, dict) else None
                if not tool_name:
                    continue
                ui_console.print_status(f"  mcp__{name}__{tool_name}")
            if client.has_capability("resources"):
                ui_console.print_status(f"  mcp__{name}__resource_list")
                ui_console.print_status(f"  mcp__{name}__resource_read")
            cached_prompts = getattr(client, "_prompts", None) or []
            for prompt in cached_prompts:
                prompt_name = prompt.get("name") if isinstance(prompt, dict) else None
                if not prompt_name:
                    continue
                ui_console.print_status(f"  /mcp:{name}:{prompt_name}")
        if not any_server:
            ui_console.print_status("(no MCP servers running)")
        return
    if sub == "reload":
        if len(tokens) < 3:
            ui_console.print_error("Usage: /mcp reload <name>")
            return
        target = tokens[2]
        try:
            mcp_manager.reload(target)
        except MCPError as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"Server {target!r} is now UNHEALTHY.")
            return
        except Exception as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"Server {target!r} is now UNHEALTHY.")
            return
        ui_console.print_status(f"Server {target!r} reloaded.")
        return
    ui_console.print_error(f"Unknown /mcp subcommand: {sub}. Use status, list, or reload.")


def _parse_mcp_prompt_command(text: str) -> tuple[str, str, dict[str, str]] | None:
    """Parse ``/mcp:<server>:<prompt> [k=v ...]`` into (server, prompt, args).

    Returns ``None`` if the command is malformed; logging is the caller's job
    so callers can surface UI feedback consistently.
    """
    if not text.startswith("/mcp:"):
        return None
    rest = text[len("/mcp:") :]
    head, _, tail = rest.partition(" ")
    if ":" not in head:
        return None
    server_name, prompt_name = head.split(":", 1)
    server_name = server_name.strip()
    prompt_name = prompt_name.strip()
    if not server_name or not prompt_name:
        return None
    arguments: dict[str, str] = {}
    for tok in tail.split():
        if "=" not in tok:
            _log.warning("Ignoring malformed /mcp: argument %r (expected key=value)", tok)
            continue
        k, _sep, v = tok.partition("=")
        if not k:
            _log.warning("Ignoring malformed /mcp: argument %r (empty key)", tok)
            continue
        arguments[k] = v
    return server_name, prompt_name, arguments


def _dispatch_mcp_prompt(
    text: str,
    *,
    mcp_manager: MCPManager,
    messages: list[dict[str, Any]],
    ui_console: AgentConsole,
) -> bool:
    """Handle a ``/mcp:`` slash command. Returns True if messages were appended.

    On success the parsed ``prompts/get`` result is converted into transcript
    messages and appended; the caller is then expected to trigger the next
    ``agent_loop()`` iteration just like a normal user input.
    """
    parsed = _parse_mcp_prompt_command(text)
    if parsed is None:
        ui_console.print_error(_MCP_PROMPT_USAGE)
        return False
    server_name, prompt_name, arguments = parsed

    client = mcp_manager.get_client(server_name)
    if client is None:
        ui_console.print_error(f"Error: MCP server {server_name!r} is not running")
        return False
    if not client.has_capability("prompts"):
        ui_console.print_error(f"Error: server {server_name!r} does not support prompts")
        return False

    try:
        result = client.get_prompt(prompt_name, arguments)
    except MCPCallError as exc:
        ui_console.print_error(str(exc))
        return False
    except Exception as exc:  # pragma: no cover — defensive
        ui_console.print_error(f"Error: {type(exc).__name__}: {exc}")
        return False

    raw_messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(raw_messages, list) or not raw_messages:
        ui_console.print_error(
            f"Error: prompt {prompt_name!r} from {server_name!r} returned no messages"
        )
        return False

    appended = False
    for entry in raw_messages:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if isinstance(content, list):
            blocks = content
        elif isinstance(content, dict):
            blocks = [content]
        elif isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        else:
            blocks = []
        text_body = _mcp_flatten_content(blocks)
        messages.append({"role": role, "content": text_body})
        appended = True

    return appended


def _drain_team_mailbox(
    ui_console: AgentConsole,
    *,
    message_bus: MessageBus,
    since: str | None,
) -> str | None:
    messages = message_bus.receive(MAIN_AGENT_NAME, since_id=since)
    if not messages:
        return since

    for message in messages:
        protocol, content = decode_protocol_content(message.content)
        label = f"Team {message.msg_type} from {message.from_agent}"
        if protocol is not None:
            label = f"{label} [{protocol.value}]"
        ui_console.print_status(f"{label}: {content}")

    return messages[-1].id


def _broadcast_team_shutdown(message_bus: MessageBus) -> None:
    ProtocolFSM(message_bus, MAIN_AGENT_NAME).broadcast(
        Protocol.SHUTDOWN,
        "BareAgent main session is shutting down.",
    )


def _read_stdio_input() -> str:
    prompt = "bareagent> " if sys.stdin.isatty() and sys.stdout.isatty() else ""
    return input(prompt)


def _cycle_permission_mode(permission: PermissionGuard) -> PermissionMode:
    current_index = _MODE_CYCLE.index(permission.mode)
    next_mode = _MODE_CYCLE[(current_index + 1) % len(_MODE_CYCLE)]
    permission.mode = next_mode
    return next_mode


def _build_stdio_read_fn(
    workspace_path: Path,
    permission: PermissionGuard,
) -> Callable[[], str]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _read_stdio_input

    try:
        from src.ui.prompt import AgentPrompt

        agent_prompt = AgentPrompt(
            commands=list(_SLASH_COMMANDS),
            history_file=workspace_path / ".bareagent_history",
            get_mode_label=lambda: permission.mode.value.upper(),
            cycle_mode=lambda: _cycle_permission_mode(permission).value.upper(),
        )
        return agent_prompt.read_input
    except Exception:
        return lambda: input(f"[{permission.mode.value.upper()}] bareagent> ")


def _clear_stdio_screen(ui_console: AgentConsole) -> None:
    if getattr(ui_console.console, "is_terminal", False):
        ui_console.console.clear(home=True)


def _print_stdio_user_message(ui_console: AgentConsole, text: str) -> None:
    if not text.strip():
        return

    from src.ui.theme import get_theme

    ui_console.console.print(
        f"> {text}",
        style=f"bold {get_theme().palette.accent}",
        markup=False,
    )


def _replay_stdio_transcript(
    messages: list[dict[str, Any]],
    ui_console: AgentConsole,
) -> None:
    tool_name_by_id: dict[str, str] = {}

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            continue

        if role == "user":
            if isinstance(content, str):
                _print_stdio_user_message(ui_console, content)
                continue
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_name = tool_name_by_id.get(
                        str(block.get("tool_use_id", "")),
                        "unknown",
                    )
                    ui_console.print_tool_result(tool_name, block.get("content", ""))
                continue

        if role != "assistant":
            continue

        if isinstance(content, str):
            ui_console.print_assistant(content)
            continue
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_value = str(block.get("text", ""))
                if text_value:
                    text_parts.append(text_value)
                continue
            if block.get("type") != "tool_use":
                continue

            tool_name = str(block.get("name", "unknown"))
            tool_id = str(block.get("id", ""))
            if tool_id:
                tool_name_by_id[tool_id] = tool_name

            if text_parts:
                ui_console.print_assistant("\n".join(text_parts))
                text_parts = []
            ui_console.print_tool_call(tool_name, block.get("input", {}))

        if text_parts:
            ui_console.print_assistant("\n".join(text_parts))


def _handle_mode_selection_stdio(
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    lines = ["Permission modes:"]
    for idx, mode in enumerate(_MODE_CYCLE, 1):
        marker = "*" if mode == permission.mode else " "
        lines.append(f"  {marker} {idx}) {mode.value:<10} {_MODE_DESCRIPTIONS[mode]}")
    ui_console.print_status("\n".join(lines))
    ui_console.print_status(f"Select [1-{len(_MODE_CYCLE)}] on the next prompt.")
    valid_choices = {str(i) for i in range(1, len(_MODE_CYCLE) + 1)}
    try:
        choice = _read_stdio_input().strip()
    except (EOFError, KeyboardInterrupt):
        ui_console.print_status("Mode selection cancelled.")
        return

    if choice in valid_choices:
        old = permission.mode
        permission.mode = _MODE_CYCLE[int(choice) - 1]
        ui_console.print_status(f"Permission mode: {old.value} → {permission.mode.value}")
        return

    ui_console.print_status("Invalid choice, mode unchanged.")


def _install_mcp_cleanup(mcp_manager: MCPManager) -> None:
    """Register exit-time + SIGTERM hooks so MCP subprocesses are reaped.

    ``atexit`` catches ``return`` from ``main()`` and any ``sys.exit()``;
    the SIGTERM handler converts a polite termination into ``sys.exit(130)``
    so ``atexit`` actually fires (raw SIGTERM bypasses it). SIGINT is
    intentionally left alone — prompt-toolkit + the existing
    ``KeyboardInterrupt`` handling in the REPL loop already cover Ctrl+C.
    """
    atexit.register(mcp_manager.close_all)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    except (ValueError, OSError):  # pragma: no cover — non-main thread / unsupported OS
        pass


def _install_lsp_cleanup(lsp_manager: LanguageServerManager) -> None:
    """Register an idempotent atexit hook for the LSP manager.

    Deliberately decoupled from :func:`_install_mcp_cleanup`: ``atexit`` fires
    callbacks LIFO regardless of registration order, and
    ``lsp_manager.close_all`` is guaranteed idempotent (see manager docstring)
    so duplicate registration from a future caller is safe. The SIGTERM
    handler is already installed by the MCP path; we rely on that to convert
    the signal into ``sys.exit(130)`` so this atexit hook actually fires.
    """
    atexit.register(lsp_manager.close_all)


_LSP_COMMAND_USAGE = "Usage: /lsp <status|list|reload <language>>"


def _dispatch_lsp_command(
    text: str,
    *,
    lsp_manager: LanguageServerManager,
    ui_console: AgentConsole,
) -> None:
    """Handle the space-prefixed ``/lsp <subcommand>`` REPL command.

    Mirrors the ``/mcp`` command shape: ``status`` shows per-server health,
    ``list`` enumerates the ``lsp_*`` tools available right now (only for
    RUNNING servers), and ``reload <language>`` rebuilds one server.
    Feedback flows through ``ui_console``; the routine never raises.
    """
    tokens = text.split()
    if len(tokens) <= 1:
        ui_console.print_status(_LSP_COMMAND_USAGE)
        return
    sub = tokens[1]
    if sub == "status":
        rows = lsp_manager.summarize()
        if not rows:
            ui_console.print_status("(no LSP servers configured)")
            return
        for row in rows:
            extensions = ", ".join(row["extensions"]) or "-"
            line = (
                f"{row['language']}: {row['status']} [{row['tool_count']} tools, ext={extensions}]"
            )
            reason = row.get("reason") or ""
            if reason:
                line = f"{line} — {reason}"
            ui_console.print_status(line)
        return
    if sub == "list":
        any_server = False
        for language, _server in lsp_manager.iter_running():
            any_server = True
            ui_console.print_status(f"[{language}]")
            # The four Tier-1 tools are uniform across servers; list them so
            # users see exactly what the LLM has access to right now.
            for tool in (
                "lsp_outline",
                "lsp_definition",
                "lsp_references",
                "lsp_diagnostics",
            ):
                ui_console.print_status(f"  {tool}")
        if not any_server:
            ui_console.print_status("(no LSP servers running)")
        return
    if sub == "reload":
        if len(tokens) < 3:
            ui_console.print_error("Usage: /lsp reload <language>")
            return
        target = tokens[2]
        try:
            lsp_manager.reload(target)
        except LSPError as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"LSP server {target!r} is now UNHEALTHY.")
            return
        except Exception as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"LSP server {target!r} is now UNHEALTHY.")
            return
        ui_console.print_status(f"LSP server {target!r} reloaded.")
        return
    ui_console.print_error(f"Unknown /lsp subcommand: {sub}. Use status, list, or reload.")


def _run_stdio_session(
    config: Config,
    provider: BaseLLMProvider,
    *,
    workspace: Path | None = None,
    agent_console: AgentConsole | None = None,
) -> int:
    from src.ui.theme import init_theme

    init_theme(config.ui.theme)
    ui_console = agent_console or AgentConsole()
    ui_console.set_theme()
    workspace_path = (workspace or Path.cwd()).resolve()
    transcript_mgr = TranscriptManager(workspace_path / ".transcripts")
    session_id = _generate_session_id(transcript_mgr)
    interaction_logger = _build_interaction_logger(
        config,
        workspace_path,
        session_id,
    )
    _configure_tracing(
        config,
        session_id=session_id,
        interaction_logger=interaction_logger,
    )
    viewer_server = None
    todo_manager = TodoManager()
    task_manager = _load_task_manager(workspace_path, ui_console)
    bg_manager = BackgroundManager()
    teammate_manager = _load_teammate_manager(workspace_path, ui_console)
    skill_loader = SkillLoader(resolve_skills_dir())
    memory_manager = _build_memory_manager(config, workspace_path, ui_console)
    message_bus, main_mailbox_cursor = _switch_session_mailbox(
        workspace_path,
        session_id,
    )
    spawned_agents: dict[str, AutonomousAgent] = {}
    messages = _initial_messages(
        workspace_path,
        skill_summary=skill_loader.get_skill_list_prompt(),
        memory_context=_memory_context(memory_manager),
    )
    mcp_manager = MCPManager(config.mcp, console=ui_console, notifier=bg_manager)
    mcp_manager.start_all()
    _install_mcp_cleanup(mcp_manager)
    lsp_manager = LanguageServerManager(
        config.lsp,
        console=ui_console,
        repository_root=str(workspace_path),
        notifier=bg_manager,
    )
    lsp_manager.start_all()
    _install_lsp_cleanup(lsp_manager)
    tools = get_tools(mcp_manager, lsp_manager)
    permission = _build_permission_guard(config)
    _install_stdio_permission_prompt(permission, ui_console)
    read_fn = _build_stdio_read_fn(workspace_path, permission)
    base_compact_fn = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        session_id=session_id,
    )
    compact_fn = _build_loop_compact(
        base_compact_fn,
        todo_manager,
        memory_manager=memory_manager,
        recall_k=config.memory.recall_k,
    )
    handlers = _build_handlers(
        workspace_path=workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        provider=provider,
        tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        messages=messages,
        config=config,
        runtime_id=session_id,
        teammate_manager=teammate_manager,
        message_bus=message_bus,
        spawned_agents=spawned_agents,
        agent_name=MAIN_AGENT_NAME,
        mcp_manager=mcp_manager,
        lsp_manager=lsp_manager,
        memory_manager=memory_manager,
    )

    ui_console.console.print(
        f"BareAgent REPL ({config.provider.name}/{config.provider.model})",
        style="bold cyan",
    )
    ui_console.print_status(
        f"Permission mode: {permission.mode.value}. Type /help to see available commands."
    )

    try:
        while True:
            main_mailbox_cursor = _drain_team_mailbox(
                ui_console,
                message_bus=message_bus,
                since=main_mailbox_cursor,
            )
            try:
                user_input = read_fn()
            except (KeyboardInterrupt, EOFError):
                _broadcast_team_shutdown(message_bus)
                ui_console.print_status("\nExiting BareAgent.")
                return 0

            text = user_input.strip()
            if not text:
                continue
            if text == "/exit":
                _broadcast_team_shutdown(message_bus)
                ui_console.print_status("Exiting BareAgent.")
                return 0
            if text == "/help":
                ui_console.print_status(_HELP_TEXT)
                continue
            if text in ("/clear", "/new"):
                if text == "/clear":
                    _clear_stdio_screen(ui_console)
                messages[:] = _initial_messages(
                    workspace_path,
                    skill_summary=skill_loader.get_skill_list_prompt(),
                    memory_context=_memory_context(memory_manager),
                )
                todo_manager.reset()
                new_session_id = _generate_session_id(
                    transcript_mgr,
                    reserved_ids={_get_compact_session_id(compact_fn)},
                )
                _set_compact_session_id(compact_fn, new_session_id)
                _set_interaction_logger_session(interaction_logger, new_session_id)
                message_bus, main_mailbox_cursor = _switch_session_mailbox(
                    workspace_path,
                    new_session_id,
                    current_bus=message_bus,
                )
                spawned_agents = {}
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=new_session_id,
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                )
                ui_console.print_status("New conversation started.")
                continue
            if text == "/compact":
                compact_fn(messages, force=True)
                _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                ui_console.print_status("Context compaction finished.")
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=_get_compact_session_id(compact_fn),
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                )
                continue
            if text == "/sessions":
                sessions = transcript_mgr.list_sessions()
                if not sessions:
                    ui_console.print_status("No saved sessions.")
                else:
                    for saved_session in sessions:
                        ui_console.console.print(saved_session)
                continue
            if text == "/resume" or text.startswith("/resume "):
                _, _, raw_session_id = text.partition(" ")
                requested_session = raw_session_id.strip() or None
                try:
                    restored_messages = transcript_mgr.resume(requested_session)
                except FileNotFoundError as exc:
                    ui_console.print_error(str(exc))
                    continue
                messages[:] = restored_messages
                resumed_session = requested_session or transcript_mgr.get_latest_session()
                if resumed_session is not None:
                    _set_compact_session_id(compact_fn, resumed_session)
                    _set_interaction_logger_session(
                        interaction_logger,
                        resumed_session,
                    )
                    message_bus, main_mailbox_cursor = _switch_session_mailbox(
                        workspace_path,
                        resumed_session,
                        current_bus=message_bus,
                    )
                    spawned_agents = {}
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=_get_compact_session_id(compact_fn),
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                )
                _replay_stdio_transcript(messages, ui_console)
                ui_console.print_status(f"Resumed session: {resumed_session}")
                continue
            if text == "/log" or text.startswith("/log "):
                viewer_server = _handle_log_command(
                    text,
                    config=config,
                    interaction_logger=interaction_logger,
                    viewer_server=viewer_server,
                    print_status=ui_console.print_status,
                )
                continue
            if text in _PERMISSION_SLASH:
                old = permission.mode
                permission.mode = _PERMISSION_SLASH[text]
                ui_console.print_status(f"Permission mode: {old.value} → {permission.mode.value}")
                continue
            if text == "/mode":
                _handle_mode_selection_stdio(permission, ui_console)
                continue
            if text == "/theme" or text.startswith("/theme "):
                from src.ui.theme import (
                    format_theme_list,
                    format_unknown_theme,
                    get_theme,
                )

                _, _, theme_arg = text.partition(" ")
                theme_name = theme_arg.strip()
                tm = get_theme()
                if not theme_name:
                    ui_console.print_status(format_theme_list(tm))
                    continue
                if tm.switch(theme_name):
                    ui_console.set_theme(tm)
                    ui_console.print_status(f"Theme switched to: {theme_name}")
                    continue
                ui_console.print_error(format_unknown_theme(theme_name))
                continue
            if text == "/team" or text.startswith("/team "):
                _handle_team_command(
                    text,
                    ui_console,
                    teammate_manager=teammate_manager,
                    team_handlers=handlers,
                )
                continue
            if text == "/mcp" or (text.startswith("/mcp ") and not text.startswith("/mcp:")):
                _dispatch_mcp_command(
                    text,
                    mcp_manager=mcp_manager,
                    ui_console=ui_console,
                )
                continue
            if text == "/lsp" or text.startswith("/lsp "):
                _dispatch_lsp_command(
                    text,
                    lsp_manager=lsp_manager,
                    ui_console=ui_console,
                )
                continue
            if text.startswith("/mcp:"):
                snapshot_len = len(messages)
                appended = _dispatch_mcp_prompt(
                    text,
                    mcp_manager=mcp_manager,
                    messages=messages,
                    ui_console=ui_console,
                )
                if not appended:
                    continue
                # Re-render the injected user turn(s) so the screen matches the
                # transcript before agent_loop runs.
                _replay_stdio_transcript(messages[snapshot_len:], ui_console)
                if messages[-1].get("role") != "user":
                    # Trailing assistant message — no LLM call needed; prompt for input.
                    ui_console.print_status("Prompt injected. Type your follow-up to continue.")
                    continue
                try:
                    agent_loop(
                        provider=provider,
                        messages=messages,
                        tools=tools,
                        handlers=handlers,
                        permission=permission,
                        compact_fn=compact_fn,
                        bg_manager=bg_manager,
                        stream=config.ui.stream,
                        console=ui_console,
                        interaction_logger=interaction_logger,
                    )
                    _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                    main_mailbox_cursor = _drain_team_mailbox(
                        ui_console,
                        message_bus=message_bus,
                        since=main_mailbox_cursor,
                    )
                except LLMCallError:
                    del messages[snapshot_len:]
                    ui_console.print_error("LLM call failed, please try again.")
                except KeyboardInterrupt:
                    del messages[snapshot_len:]
                    ui_console.print_status("Agent loop interrupted.")
                continue
            if text == "/remember" or text.startswith("/remember "):
                if memory_manager is None:
                    ui_console.print_error(
                        "Persistent memory is disabled (enable [memory] in config)."
                    )
                    continue
                _, _, remember_arg = text.partition(" ")
                # Rewrite into an LLM instruction and fall through to the
                # normal user-turn handling below, which runs agent_loop.
                text = build_remember_instruction(remember_arg.strip())
            elif text == "/forget" or text.startswith("/forget "):
                if memory_manager is None:
                    ui_console.print_error(
                        "Persistent memory is disabled (enable [memory] in config)."
                    )
                    continue
                _, _, forget_arg = text.partition(" ")
                text = build_forget_instruction(forget_arg.strip())

            messages.append({"role": "user", "content": text})
            snapshot_len = len(messages) - 1
            try:
                agent_loop(
                    provider=provider,
                    messages=messages,
                    tools=tools,
                    handlers=handlers,
                    permission=permission,
                    compact_fn=compact_fn,
                    bg_manager=bg_manager,
                    stream=config.ui.stream,
                    console=ui_console,
                    interaction_logger=interaction_logger,
                )
                _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                main_mailbox_cursor = _drain_team_mailbox(
                    ui_console,
                    message_bus=message_bus,
                    since=main_mailbox_cursor,
                )
            except LLMCallError:
                del messages[snapshot_len:]
                ui_console.print_error("LLM call failed, please try again.")
            except KeyboardInterrupt:
                del messages[snapshot_len:]
                ui_console.print_status("Agent loop interrupted.")
    finally:
        try:
            mcp_manager.close_all()
        except Exception:
            pass
        try:
            lsp_manager.close_all()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = resolve_config_path(args.config)

    try:
        config = load_config(
            config_path,
            provider_override=args.provider,
            model_override=args.model,
        )
    except FileNotFoundError:
        print(f"Config file not found: {config_path}")
        return 1
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Failed to load config: {exc}")
        return 1

    try:
        provider = create_provider(config)
    except ValueError as exc:
        print(f"Failed to initialize provider: {exc}")
        return 1

    return _run_stdio_session(config, provider)


if __name__ == "__main__":
    raise SystemExit(main())
