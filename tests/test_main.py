import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import src.main as main_module
from rich.console import Console
from src.main import (
    DEFAULT_CONFIG_PATH,
    Config,
    DebugConfig,
    PermissionConfig,
    ProviderConfig,
    SubagentConfig,
    UIConfig,
    _refresh_nag_reminder,
    load_config,
    resolve_config_path,
)
from src.core.fileutil import is_tool_result_message
from src.debug.interaction_log import InteractionLogger
from src.memory.transcript import TranscriptManager
from src.provider.base import ThinkingConfig
from src.ui.console import AgentConsole
from tests.conftest import make_test_config


def test_resolve_config_path_uses_bundled_config_outside_project_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAREAGENT_CONFIG", raising=False)

    assert resolve_config_path(None) == DEFAULT_CONFIG_PATH


def test_resolve_config_path_prefers_environment_override(monkeypatch) -> None:
    override = Path("custom-config.toml")
    monkeypatch.setenv("BAREAGENT_CONFIG", str(override))

    assert resolve_config_path(None) == override


def test_load_config_uses_matching_default_api_key_env_for_provider_override(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "anthropic"',
                'model = "claude-sonnet-4-20250514"',
                'api_key_env = "ANTHROPIC_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, provider_override="openai")

    assert config.provider.name == "openai"
    assert config.provider.api_key_env == "OPENAI_API_KEY"
    assert config.provider.wire_api is None


def test_load_config_reads_provider_wire_api(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                'base_url = "https://right.codes/codex/v1"',
                'wire_api = "responses"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider.base_url == "https://right.codes/codex/v1"
    assert config.provider.wire_api == "responses"


def test_load_config_reads_subagent_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[subagent]',
                'max_depth = 5',
                'default_type = "plan"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.subagent.max_depth == 5
    assert config.subagent.default_type == "plan"


def test_load_config_reads_debug_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[debug]',
                'enabled = true',
                'log_dir = "debug-output"',
                'viewer_port = 9001',
                'pretty = false',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.debug == DebugConfig(
        enabled=True,
        log_dir="debug-output",
        viewer_port=9001,
        pretty=False,
    )


def test_load_config_debug_env_overrides(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[debug]',
                'enabled = false',
                'log_dir = "debug-output"',
                'viewer_port = 9001',
                'pretty = false',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAREAGENT_DEBUG", "1")
    monkeypatch.setenv("BAREAGENT_DEBUG_LOG_DIR", "env-logs")
    monkeypatch.setenv("BAREAGENT_DEBUG_VIEWER_PORT", "8123")
    monkeypatch.setenv("BAREAGENT_DEBUG_PRETTY", "true")

    config = load_config(config_path)

    assert config.debug == DebugConfig(
        enabled=True,
        log_dir="env-logs",
        viewer_port=8123,
        pretty=True,
    )


def test_load_config_rejects_unknown_subagent_default_type(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[subagent]',
                'default_type = "plan-typo"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_config(config_path)
    except ValueError as exc:
        assert "subagent.default_type" in str(exc)
    else:
        raise AssertionError(
            "Expected load_config() to reject an unknown subagent.default_type"
        )


def test_make_teammate_provider_factory_inherits_custom_api_key_env(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_create_provider(config):
        captured["config"] = config
        return "provider"

    monkeypatch.setattr("src.main.create_provider", _fake_create_provider)
    config = Config(
        provider=ProviderConfig(
            name="openai",
            model="gpt-5-codex-mini",
            api_key_env="MY_OPENAI_KEY",
        ),
        permission=PermissionConfig(mode="default", allow=[], deny=[]),
        ui=UIConfig(stream=False, theme="dark"),
        subagent=SubagentConfig(max_depth=3, default_type="general-purpose"),
        thinking=ThinkingConfig(),
        path=Path("config.toml"),
        debug=DebugConfig(),
    )

    factory = main_module._make_teammate_provider_factory(config)
    provider = factory({})

    assert provider == "provider"
    assert captured["config"].provider.api_key_env == "MY_OPENAI_KEY"  # type: ignore[index, union-attr]


def test_generate_session_id_avoids_saved_and_reserved_collisions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    transcript_dir = tmp_path / ".transcripts"
    transcript_dir.mkdir()
    existing_session_id = "20260404-120000-123456-abc123"
    transcript_path = transcript_dir / f"{existing_session_id}_2026-04-04T12-00-00.jsonl"
    transcript_path.write_text(
        json.dumps({"role": "user", "content": "saved"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    class FrozenDatetime:
        @classmethod
        def now(cls) -> datetime:
            return datetime(2026, 4, 4, 12, 0, 0, 123456)

    suffixes = iter(["abc123", "def456"])
    monkeypatch.setattr(main_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        main_module,
        "generate_random_id",
        lambda _length=6: next(suffixes),
    )

    session_id = main_module._generate_session_id(
        TranscriptManager(transcript_dir),
        reserved_ids={existing_session_id},
    )

    assert session_id == "20260404-120000-123456-def456"


def test_slash_new_appears_in_slash_commands() -> None:
    assert "/new" in main_module._SLASH_COMMANDS


def test_slash_theme_appears_after_mode_in_slash_commands() -> None:
    mode_index = main_module._SLASH_COMMANDS.index("/mode")

    assert main_module._SLASH_COMMANDS[mode_index + 1] == "/theme"


def test_help_text_describes_theme_command() -> None:
    assert (
        "  /theme     Switch color theme "
        "(catppuccin-mocha, dracula, nord, tokyo-night, gruvbox)\n"
        in main_module._HELP_TEXT
    )


def test_slash_log_appears_in_slash_commands() -> None:
    assert "/log" in main_module._SLASH_COMMANDS


def test_help_text_describes_log_command() -> None:
    assert "  /log       Debug log viewer (status|serve|open|<seq>)\n" in main_module._HELP_TEXT


def test_handle_log_command_reports_disabled_debug(tmp_path: Path) -> None:
    config = make_test_config(tmp_path)
    messages: list[str] = []

    viewer_server = main_module._handle_log_command(
        "/log",
        config=config,
        interaction_logger=None,
        viewer_server=None,
        print_status=messages.append,
    )

    assert viewer_server is None
    assert messages == [
        "Debug logging is disabled. Set [debug] enabled = true in config.toml "
        "or BAREAGENT_DEBUG=1"
    ]


def test_handle_log_command_reports_status_and_interaction_summary(
    tmp_path: Path,
) -> None:
    config = make_test_config(tmp_path)
    config.debug = DebugConfig(enabled=True, log_dir=".logs", viewer_port=8321, pretty=True)
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    seq = logger.log_request(
        [{"role": "user", "content": "hello"}],
        [{"name": "echo"}],
    )
    logger.log_response(
        seq,
        thinking="a" * 120,
        input_tokens=7,
        output_tokens=9,
        duration_ms=45,
        tool_calls=[{"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}],
    )
    messages: list[str] = []

    main_module._handle_log_command(
        "/log status",
        config=config,
        interaction_logger=logger,
        viewer_server=None,
        print_status=messages.append,
    )
    main_module._handle_log_command(
        "/log 0",
        config=config,
        interaction_logger=logger,
        viewer_server=None,
        print_status=messages.append,
    )

    assert messages[0] == "\n".join(
        [
            "Debug logging: enabled",
            "Log dir: .logs",
            "Current session: sess-1",
            "Interactions: 1",
            "Total tokens: 16",
            "Sessions: 1",
            "Viewer: not running (use /log serve)",
        ]
    )
    assert messages[1].startswith("Interaction #0:\n")
    assert "Input tokens:  7" in messages[1]
    assert "Output tokens: 9" in messages[1]
    assert "Duration:      45ms" in messages[1]
    assert "Tool calls:    1" in messages[1]
    assert "Thinking: " in messages[1]
    assert "..." in messages[1]


def test_handle_log_command_serves_and_opens_viewer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = make_test_config(tmp_path)
    config.debug = DebugConfig(enabled=True, log_dir=".logs", viewer_port=9001, pretty=True)
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    started: list[tuple[InteractionLogger, int]] = []
    opened: list[str] = []
    server_token = object()

    def _fake_start(logger_arg: InteractionLogger, config_arg: Config) -> object:
        started.append((logger_arg, config_arg.debug.viewer_port))
        return server_token

    monkeypatch.setattr(main_module, "_start_debug_viewer", _fake_start)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    messages: list[str] = []

    viewer_server = main_module._handle_log_command(
        "/log serve",
        config=config,
        interaction_logger=logger,
        viewer_server=None,
        print_status=messages.append,
    )
    viewer_server = main_module._handle_log_command(
        "/log open",
        config=config,
        interaction_logger=logger,
        viewer_server=viewer_server,
        print_status=messages.append,
    )

    assert viewer_server is server_token
    assert started == [(logger, 9001)]
    assert opened == ["http://127.0.0.1:9001"]
    assert messages == [
        "Debug viewer started at http://127.0.0.1:9001",
        "Opening http://127.0.0.1:9001 in browser...",
    ]


def test_main_falls_back_to_stdio_when_textual_ui_is_unavailable(
    monkeypatch,
) -> None:
    config = SimpleNamespace()
    provider = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(main_module, "parse_args", lambda argv=None: SimpleNamespace(
        config=None,
        provider=None,
        model=None,
    ))
    monkeypatch.setattr(main_module, "resolve_config_path", lambda path: Path("config.toml"))
    monkeypatch.setattr(main_module, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(main_module, "create_provider", lambda loaded: provider)
    monkeypatch.setattr(main_module, "_supports_textual_ui", lambda: False)

    def _fake_stdio(config_arg, provider_arg):
        captured["config"] = config_arg
        captured["provider"] = provider_arg
        return 7

    monkeypatch.setattr(main_module, "_run_stdio_session", _fake_stdio)

    assert main_module.main([]) == 7
    assert captured == {"config": config, "provider": provider}


def test_stdio_theme_switch_preserves_injected_console(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = make_test_config(tmp_path)
    output_buffer = StringIO()
    agent_console = AgentConsole(
        Console(
            file=output_buffer,
            force_terminal=False,
            color_system=None,
            width=100,
        )
    )
    inputs = iter(["/theme dracula", "/exit"])

    class _FakeSkillLoader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_skill_list_prompt(self) -> str:
            return ""

    monkeypatch.setattr(main_module, "_read_stdio_input", lambda: next(inputs))
    monkeypatch.setattr(main_module, "_generate_session_id", lambda *_args, **_kwargs: "session-1")
    monkeypatch.setattr(main_module, "_load_task_manager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_module, "_load_teammate_manager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_module, "_switch_session_mailbox", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(main_module, "_initial_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_module, "get_tools", lambda: [])
    monkeypatch.setattr(main_module, "SkillLoader", _FakeSkillLoader)
    monkeypatch.setattr(main_module, "resolve_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(main_module, "Compactor", lambda **_kwargs: object())
    monkeypatch.setattr(
        main_module,
        "_build_loop_compact",
        lambda *_args, **_kwargs: (lambda _messages, force=False: None),
    )
    monkeypatch.setattr(main_module, "_build_handlers", lambda **_kwargs: {})
    monkeypatch.setattr(main_module, "_drain_team_mailbox", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_broadcast_team_shutdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_save_transcript_snapshot", lambda *_args, **_kwargs: None)

    assert main_module._run_stdio_session(config, object(), agent_console=agent_console) == 0

    rendered = output_buffer.getvalue()
    assert "BareAgent REPL" in rendered
    assert "Theme switched to: dracula" in rendered
    assert "Exiting BareAgent." in rendered


def test_run_stdio_session_passes_interaction_logger_when_debug_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = make_test_config(tmp_path)
    config.debug = DebugConfig(enabled=True, log_dir=".logs", pretty=True)
    captured: dict[str, object] = {}
    inputs = iter(["hello", "/exit"])

    class _FakeSkillLoader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_skill_list_prompt(self) -> str:
            return ""

    monkeypatch.setattr(main_module, "_read_stdio_input", lambda: next(inputs))
    monkeypatch.setattr(main_module, "_generate_session_id", lambda *_args, **_kwargs: "session-1")
    monkeypatch.setattr(main_module, "_load_task_manager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_module, "_load_teammate_manager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_module, "_switch_session_mailbox", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(main_module, "_initial_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_module, "get_tools", lambda: [])
    monkeypatch.setattr(main_module, "SkillLoader", _FakeSkillLoader)
    monkeypatch.setattr(main_module, "resolve_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(main_module, "Compactor", lambda **_kwargs: object())
    monkeypatch.setattr(
        main_module,
        "_build_loop_compact",
        lambda *_args, **_kwargs: (lambda _messages, force=False: None),
    )
    monkeypatch.setattr(main_module, "_build_handlers", lambda **_kwargs: {})
    monkeypatch.setattr(main_module, "_drain_team_mailbox", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_broadcast_team_shutdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_save_transcript_snapshot", lambda *_args, **_kwargs: None)

    def _fake_agent_loop(**kwargs):
        captured["interaction_logger"] = kwargs.get("interaction_logger")
        return "done"

    monkeypatch.setattr(main_module, "agent_loop", _fake_agent_loop)

    assert main_module._run_stdio_session(config, object(), workspace=tmp_path) == 0
    assert captured["interaction_logger"] is not None
    assert captured["interaction_logger"].session_id == "session-1"  # type: ignore[union-attr]


def test_nag_reminder_skips_tool_result_messages() -> None:
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "do something"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling tool"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "file.txt",
                },
            ],
        },
    ]
    _refresh_nag_reminder(messages, "Remember to be concise.")

    nag_indices = [
        i
        for i, message in enumerate(messages)
        if message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and "<nag-reminder>" in str(message["content"])
    ]
    assert len(nag_indices) == 1
    nag_idx = nag_indices[0]
    assert messages[nag_idx - 1].get("content") == "do something"

    assistant_idx = next(
        i for i, message in enumerate(messages) if message.get("role") == "assistant"
    )
    tool_result_msg = messages[assistant_idx + 1]
    assert tool_result_msg.get("role") == "user"
    assert is_tool_result_message(tool_result_msg)
