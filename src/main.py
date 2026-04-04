from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout

from src.concurrency.background import BackgroundManager
from src.core.context import assemble_system_prompt
from src.core.loop import agent_loop, LLMCallError
from src.core.tools import get_handlers, get_tools
from src.memory.compact import Compactor
from src.memory.transcript import TranscriptManager
from src.permission.guard import PermissionGuard, PermissionMode
from src.planning.agent_types import BUILTIN_AGENT_TYPES, DEFAULT_AGENT_TYPE
from src.planning.skills import SkillLoader, resolve_skills_dir
from src.planning.tasks import TaskManager
from src.planning.todo import TodoManager
from src.permission.rules import parse_permission_rules
from src.provider.base import BaseLLMProvider, ThinkingConfig
from src.provider.factory import create_provider
from src.team.autonomous import AutonomousAgent
from src.team.mailbox import Message, MessageBus, optional_string as _coerce_optional_string
from src.team.manager import TeammateManager
from src.team.protocols import Protocol, ProtocolFSM, decode_protocol_content
from src.ui.console import AgentConsole

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
VALID_PERMISSION_MODES = {"default", "auto", "plan", "bypass"}
VALID_THINKING_MODES = {"adaptive", "enabled", "disabled"}
VALID_SUBAGENT_TYPES = set(BUILTIN_AGENT_TYPES)
MAIN_AGENT_NAME = "main"
DEFAULT_API_KEY_ENV_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


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
class Config:
    provider: ProviderConfig
    permission: PermissionConfig
    ui: UIConfig
    subagent: SubagentConfig
    thinking: ThinkingConfig
    path: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bareagent")
    parser.add_argument("--provider", help="Override the configured provider name.")
    parser.add_argument("--model", help="Override the configured model name.")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to the TOML config file. Defaults to BAREAGENT_CONFIG or the bundled config.toml.",
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


def _validate_mode(name: str, value: str, allowed: set[str]) -> str:
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
        mode=_validate_mode(
            "thinking.mode",
            _resolve_string(
                thinking_raw.get("mode", "adaptive"),
                "BAREAGENT_THINKING_MODE",
            ),
            VALID_THINKING_MODES,
        ),
        budget_tokens=_resolve_int(
            int(thinking_raw.get("budget_tokens", 10000)),
            "BAREAGENT_THINKING_BUDGET_TOKENS",
        ),
    )

    return Config(
        provider=provider,
        permission=permission,
        ui=ui,
        subagent=subagent,
        thinking=thinking,
        path=config_path.resolve(),
    )


_NAG_REMINDER_PREFIX = "<nag-reminder>"


def _is_tool_result_message(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _initial_messages(workspace: Path, skill_summary: str = "") -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": assemble_system_prompt(workspace, skill_summary=skill_summary),
        }
    ]


def _refresh_nag_reminder(
    messages: list[dict[str, str | list[dict[str, str]]]],
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
        if msg.get("role") == "user" and not _is_tool_result_message(msg):
            messages.insert(index + 1, nag_message)
            return

    messages.append(nag_message)


def _build_loop_compact(compact_fn: object, todo_manager: TodoManager):
    def _compact(messages: list[dict[str, str | list[dict[str, str]]]], force: bool = False) -> None:
        _refresh_nag_reminder(messages, todo_manager.get_nag_reminder())
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
_MODE_CYCLE = [PermissionMode.DEFAULT, PermissionMode.AUTO, PermissionMode.PLAN, PermissionMode.BYPASS]
_MODE_DESCRIPTIONS = {
    PermissionMode.DEFAULT: "Write operations require confirmation",
    PermissionMode.AUTO: "Safe commands auto-approved",
    PermissionMode.PLAN: "Read-only mode",
    PermissionMode.BYPASS: "No confirmation required",
}
_SLASH_COMMANDS = [
    "/help", "/exit", "/clear", "/compact",
    *_PERMISSION_SLASH, "/mode",
    "/sessions", "/resume", "/team",
]


def _print_mode_change(old: PermissionMode, new: PermissionMode, ui_console: AgentConsole) -> None:
    ui_console.print_status(f"Permission mode: {old.value} → {new.value}")


def _next_permission_mode(current: PermissionMode) -> PermissionMode:
    current_idx = _MODE_CYCLE.index(current)
    return _MODE_CYCLE[(current_idx + 1) % len(_MODE_CYCLE)]


def _handle_shift_tab_mode_cycle(
    _event: object,
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    """Cycle permission mode without mutating the current prompt buffer."""
    old = permission.mode
    permission.mode = _next_permission_mode(permission.mode)
    _print_mode_change(old, permission.mode, ui_console)


def _handle_mode_interactive(
    permission: PermissionGuard,
    ui_console: AgentConsole,
    session: PromptSession | None = None,
) -> None:
    """Display an interactive menu for selecting permission mode."""
    lines = ["Permission modes:"]
    for idx, mode in enumerate(_MODE_CYCLE, 1):
        marker = "*" if mode == permission.mode else " "
        lines.append(f"  {marker} {idx}) {mode.value:<10} {_MODE_DESCRIPTIONS[mode]}")
    ui_console.print_status("\n".join(lines))
    ui_console.print_status(f"Select [1-{len(_MODE_CYCLE)}] on the next prompt.")
    valid_choices = {str(i) for i in range(1, len(_MODE_CYCLE) + 1)}
    try:
        choice = _read_user_input(session).strip()
    except (EOFError, KeyboardInterrupt):
        ui_console.print_status("Mode selection cancelled.")
        return
    if choice in valid_choices:
        old = permission.mode
        permission.mode = _MODE_CYCLE[int(choice) - 1]
        _print_mode_change(old, permission.mode, ui_console)
    else:
        ui_console.print_status("Invalid choice, mode unchanged.")


class _SlashCompleter(Completer):
    """当输入以 / 开头时自动补全斜杠命令。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd in _SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text))


def _supports_prompt_toolkit() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_user_input(session: PromptSession | None) -> str:
    if session is None:
        return input("bareagent> ")

    with patch_stdout():
        return session.prompt("bareagent> ")


def run_repl(
    config: Config,
    provider: BaseLLMProvider,
    workspace: Path | None = None,
    agent_console: AgentConsole | None = None,
) -> int:
    ui_console = agent_console or AgentConsole()
    workspace_path = (workspace or Path.cwd()).resolve()
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    _kb = KeyBindings()

    @_kb.add(Keys.ControlZ)
    def _handle_ctrl_z(event):
        """Ctrl+Z immediately raises EOFError to exit the REPL."""
        event.app.exit(exception=EOFError())

    @_kb.add(Keys.BackTab)
    def _handle_shift_tab(event):
        """Shift+Tab cycles through permission modes."""
        _handle_shift_tab_mode_cycle(event, permission, ui_console)

    session = (
        PromptSession(
            completer=_SlashCompleter(),
            complete_while_typing=True,
            key_bindings=_kb,
        )
        if _supports_prompt_toolkit()
        else None
    )
    todo_manager = TodoManager()
    task_manager = _load_task_manager(workspace_path, ui_console)
    bg_manager = BackgroundManager()
    teammate_manager = _load_teammate_manager(workspace_path, ui_console)
    message_bus = MessageBus(workspace_path / ".mailbox")
    message_bus.ensure_mailbox(MAIN_AGENT_NAME)
    spawned_agents: dict[str, AutonomousAgent] = {}
    main_mailbox_cursor: str | None = message_bus.latest_message_id(MAIN_AGENT_NAME)
    skill_loader = SkillLoader(resolve_skills_dir())
    transcript_mgr = TranscriptManager(workspace_path / ".transcripts")
    messages = _initial_messages(
        workspace_path,
        skill_summary=skill_loader.get_skill_list_prompt(),
    )
    tools = get_tools()
    permission = _build_permission_guard(config)
    base_compact_fn = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        session_id=session_id,
    )
    compact_fn = _build_loop_compact(base_compact_fn, todo_manager)
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
        teammate_manager=teammate_manager,
        message_bus=message_bus,
        spawned_agents=spawned_agents,
        agent_name=MAIN_AGENT_NAME,
    )

    ui_console.console.print(
        f"BareAgent REPL ({config.provider.name}/{config.provider.model})",
        style="bold cyan",
    )
    ui_console.print_status(
        f"Permission mode: {permission.mode.value}. Type /help to see available commands."
    )

    ctrl_c_count = 0
    while True:
        main_mailbox_cursor = _drain_team_mailbox(
            ui_console,
            message_bus=message_bus,
            since=main_mailbox_cursor,
        )
        try:
            user_input = _read_user_input(session)
        except KeyboardInterrupt:
            ctrl_c_count += 1
            if ctrl_c_count >= 2:
                _broadcast_team_shutdown(message_bus)
                ui_console.print_status("\nExiting BareAgent.")
                return 0
            ui_console.console.print(
                "\nPress Ctrl+C again to exit, or continue typing.", style="yellow"
            )
            continue
        except EOFError:
            _broadcast_team_shutdown(message_bus)
            ui_console.print_status("\nExiting BareAgent.")
            return 0

        ctrl_c_count = 0
        text = user_input.strip()
        if not text:
            continue
        if text == "/exit":
            _broadcast_team_shutdown(message_bus)
            ui_console.print_status("Exiting BareAgent.")
            return 0
        if text == "/help":
            ui_console.print_status(
                "Available commands:\n"
                "  /help      Show this help message\n"
                "  /exit      Exit BareAgent\n"
                "  /clear     Clear the screen\n"
                "  /compact   Compress conversation context\n"
                "  /default   Switch to DEFAULT permission mode\n"
                "  /auto      Switch to AUTO permission mode\n"
                "  /plan      Switch to PLAN permission mode\n"
                "  /bypass    Switch to BYPASS permission mode\n"
                "  /mode      Interactive permission mode selection\n"
                "  /sessions  List saved sessions\n"
                "  /resume    Resume a previous session\n"
                "  /team      Manage team agents (list | spawn | send)\n"
                "  Shift+Tab  Cycle through permission modes"
            )
            continue
        if text == "/clear":
            ui_console.console.clear()
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
                teammate_manager=teammate_manager,
                message_bus=message_bus,
                spawned_agents=spawned_agents,
                agent_name=MAIN_AGENT_NAME,
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
                teammate_manager=teammate_manager,
                message_bus=message_bus,
                spawned_agents=spawned_agents,
                agent_name=MAIN_AGENT_NAME,
            )
            ui_console.print_status(f"Resumed session: {resumed_session}")
            continue
        if text in _PERMISSION_SLASH:
            old = permission.mode
            permission.mode = _PERMISSION_SLASH[text]
            _print_mode_change(old, permission.mode, ui_console)
            continue
        if text == "/mode":
            _handle_mode_interactive(permission, ui_console, session)
            continue
        if text == "/team" or text.startswith("/team "):
            _handle_team_command(
                text,
                ui_console,
                teammate_manager=teammate_manager,
                team_handlers=handlers,
            )
            continue

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
            )
            _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
            main_mailbox_cursor = _drain_team_mailbox(
                ui_console,
                message_bus=message_bus,
                since=main_mailbox_cursor,
            )
        except LLMCallError:
            del messages[snapshot_len:]
            ui_console.console.print("LLM call failed, please try again.", style="yellow")
            continue
        except KeyboardInterrupt:
            ctrl_c_count = 0
            del messages[snapshot_len:]
            ui_console.console.print("\nAgent loop interrupted.", style="yellow")
            continue


def _build_permission_guard(config: Config) -> PermissionGuard:
    guard = PermissionGuard(PermissionMode(config.permission.mode))
    guard.allow_rules = list(config.permission.allow)
    guard.deny_rules = list(config.permission.deny)
    return guard


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
    messages: list[dict[str, object]],
    compact_fn: object,
) -> None:
    transcript_mgr.save(messages, _get_compact_session_id(compact_fn))


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
    messages: list[dict[str, object]],
    config: Config,
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
    system_prompt_override: str | None = None,
) -> dict[str, object]:
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


def _extract_system_prompt(messages: list[dict[str, object]]) -> str:
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
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
) -> dict[str, object]:
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
            bg_manager.submit(f"team:{teammate_name}", autonomous_agent.run)
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
        api_key_env = str(
            provider_overrides.get(
                "api_key_env",
                inherited_api_key_env,
            )
        ).strip() or inherited_api_key_env
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
    team_handlers: dict[str, object],
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


def main(argv: list[str] | None = None) -> int:
    app_console = AgentConsole()
    args = parse_args(argv)
    config_path = resolve_config_path(args.config)

    try:
        config = load_config(
            config_path,
            provider_override=args.provider,
            model_override=args.model,
        )
    except FileNotFoundError:
        app_console.print_error(f"Config file not found: {config_path}")
        return 1
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        app_console.print_error(f"Failed to load config: {exc}")
        return 1

    try:
        provider = create_provider(config)
    except ValueError as exc:
        app_console.print_error(f"Failed to initialize provider: {exc}")
        return 1

    return run_repl(config, provider, agent_console=app_console)


if __name__ == "__main__":
    raise SystemExit(main())
