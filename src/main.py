from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from src.core.context import assemble_system_prompt
from src.core.loop import agent_loop
from src.core.tools import get_handlers, get_tools
from src.permission.guard import PermissionGuard, PermissionMode
from src.planning.skills import SkillLoader, resolve_skills_dir
from src.planning.todo import TodoManager
from src.permission.rules import parse_permission_rules
from src.provider.base import BaseLLMProvider, ThinkingConfig
from src.provider.factory import create_provider
from src.ui.console import AgentConsole

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
VALID_PERMISSION_MODES = {"default", "auto", "plan", "bypass"}
VALID_THINKING_MODES = {"adaptive", "enabled", "disabled"}
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
class Config:
    provider: ProviderConfig
    permission: PermissionConfig
    ui: UIConfig
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


def _read_config_file(config_path: Path) -> dict:
    with config_path.open("rb") as file:
        return tomllib.load(file)


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
        thinking=thinking,
        path=config_path.resolve(),
    )


_NAG_REMINDER_PREFIX = "<nag-reminder>"


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
        if messages[index].get("role") == "user":
            messages.insert(index, nag_message)
            return

    messages.append(nag_message)


def _build_nag_injector(todo_manager: TodoManager):
    def _inject(messages: list[dict[str, str | list[dict[str, str]]]]) -> None:
        _refresh_nag_reminder(messages, todo_manager.get_nag_reminder())

    return _inject


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
    session = PromptSession() if _supports_prompt_toolkit() else None
    todo_manager = TodoManager()
    skill_loader = SkillLoader(resolve_skills_dir())
    messages = _initial_messages(
        workspace_path,
        skill_summary=skill_loader.get_skill_list_prompt(),
    )
    tools = get_tools()
    permission = _build_permission_guard(config)
    handlers = get_handlers(
        workspace_path,
        todo_manager=todo_manager,
        skill_loader=skill_loader,
        provider=provider,
        tools=tools,
        permission=permission,
        subagent_system_prompt=str(messages[0]["content"]),
    )

    ui_console.console.print(
        f"BareAgent REPL ({config.provider.name}/{config.provider.model})",
        style="bold cyan",
    )
    ui_console.print_status("Use /exit to quit, /clear to clear the screen.")

    while True:
        try:
            user_input = _read_user_input(session)
        except KeyboardInterrupt:
            ui_console.console.print("\nInterrupted. Use /exit to quit.", style="yellow")
            continue
        except EOFError:
            ui_console.print_status("\nExiting BareAgent.")
            return 0

        text = user_input.strip()
        if not text:
            continue
        if text == "/exit":
            ui_console.print_status("Exiting BareAgent.")
            return 0
        if text == "/clear":
            ui_console.console.clear()
            continue

        messages.append({"role": "user", "content": text})
        try:
            agent_loop(
                provider=provider,
                messages=messages,
                tools=tools,
                handlers=handlers,
                permission=permission,
                bg_manager=_build_nag_injector(todo_manager),
                stream=config.ui.stream,
                console=ui_console,
            )
        except KeyboardInterrupt:
            ui_console.console.print("\nAgent loop interrupted.", style="yellow")
            continue


def _build_permission_guard(config: Config) -> PermissionGuard:
    guard = PermissionGuard(PermissionMode(config.permission.mode))
    guard.allow_rules = list(config.permission.allow)
    guard.deny_rules = list(config.permission.deny)
    return guard


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
