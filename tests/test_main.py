from pathlib import Path
import time

import src.main as main_module
from src.main import (
    DEFAULT_CONFIG_PATH,
    Config,
    PermissionConfig,
    ProviderConfig,
    SubagentConfig,
    UIConfig,
    _is_tool_result_message,
    _refresh_nag_reminder,
    load_config,
    resolve_config_path,
    run_repl,
)
from src.memory.transcript import TranscriptManager
from src.planning.tasks import TaskManager
from src.provider.base import BaseLLMProvider, LLMResponse, ThinkingConfig
from src.team.manager import TeammateManager


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
        raise AssertionError("Expected load_config() to reject an unknown subagent.default_type")


class ReplayProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return self.responses.pop(0)

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class DummyConsole:
    def __init__(self) -> None:
        self.console = self
        self.printed: list[str] = []
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def print(self, *args, **kwargs) -> None:
        _ = kwargs
        self.printed.append(" ".join(str(arg) for arg in args))

    def clear(self) -> None:
        self.printed.append("<clear>")

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)

    def print_assistant(self, text: str) -> None:
        self.printed.append(text)

    def print_tool_call(self, name: str, input_data: dict) -> None:
        self.printed.append(f"tool:{name}:{input_data}")

    def print_tool_result(self, name: str, output) -> None:
        self.printed.append(f"result:{name}:{output}")


def test_run_repl_persists_latest_turn_without_compaction(monkeypatch, tmp_path: Path) -> None:
    inputs = iter(["hello", "/exit"])
    monkeypatch.setattr("src.main._supports_prompt_toolkit", lambda: False)
    monkeypatch.setattr("src.main._read_user_input", lambda _session: next(inputs))

    provider = ReplayProvider(
        [
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=2,
            )
        ]
    )
    console = DummyConsole()

    exit_code = run_repl(
        _make_config(),
        provider,
        workspace=tmp_path,
        agent_console=console,
    )

    manager = TranscriptManager(tmp_path / ".transcripts")

    assert exit_code == 0
    assert manager.list_sessions()
    assert manager.resume()[-2:] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Done."},
    ]


def test_run_repl_rebinds_session_id_after_resume_for_compact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    transcript_dir = tmp_path / ".transcripts"
    transcript_dir.mkdir()
    original_messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "旧对话"},
        {"role": "assistant", "content": "旧回复"},
    ]
    manager = TranscriptManager(transcript_dir)
    manager.save(original_messages, "session-old")

    inputs = iter(["/resume session-old", "/compact", "/exit"])
    monkeypatch.setattr("src.main._supports_prompt_toolkit", lambda: False)
    monkeypatch.setattr("src.main._read_user_input", lambda _session: next(inputs))

    provider = ReplayProvider(
        [
            LLMResponse(
                text="压缩后的上下文",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=2,
            )
        ]
    )
    console = DummyConsole()

    exit_code = run_repl(
        _make_config(),
        provider,
        workspace=tmp_path,
        agent_console=console,
    )

    saved_names = [path.name for path in transcript_dir.glob("*.jsonl")]

    assert exit_code == 0
    assert all(name.startswith("session-old_") for name in saved_names)
    assert manager.load("session-old") == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "[Context Compressed]\n压缩后的上下文"},
        {"role": "assistant", "content": "收到，我已理解之前的上下文，继续工作。"},
    ]


def test_run_repl_does_not_abort_when_task_file_is_invalid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".tasks.json").write_text("{broken-json", encoding="utf-8")
    inputs = iter(["/exit"])
    monkeypatch.setattr("src.main._supports_prompt_toolkit", lambda: False)
    monkeypatch.setattr("src.main._read_user_input", lambda _session: next(inputs))

    console = DummyConsole()

    exit_code = run_repl(
        _make_config(),
        ReplayProvider([]),
        workspace=tmp_path,
        agent_console=console,
    )

    assert exit_code == 0
    assert any("Failed to load task file" in error for error in console.errors)


def test_run_repl_handles_team_spawn_and_send_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    TeammateManager(tmp_path / ".team.json").register(
        "code-reviewer",
        "code reviewer",
        "You review code changes.",
    )
    TaskManager(tmp_path / ".tasks.json").create(
        "Review module",
        description="Inspect the updated manager implementation",
    )
    inputs = iter(
        [
            (0.05, "/team spawn code-reviewer"),
            (0.05, "/team send code-reviewer Review src/main.py"),
            (0.3, "/team list"),
            (0.05, "/exit"),
        ]
    )
    monkeypatch.setattr("src.main._supports_prompt_toolkit", lambda: False)

    def _next_input(_session) -> str:
        delay, value = next(inputs)
        time.sleep(delay)
        return value

    monkeypatch.setattr("src.main._read_user_input", _next_input)
    monkeypatch.setattr(
        "src.main._make_teammate_provider_factory",
        lambda _config: (lambda _overrides: ReplayProvider(["Message handled.", "Task done."])),
    )

    console = DummyConsole()

    exit_code = run_repl(
        _make_config(),
        ReplayProvider([]),
        workspace=tmp_path,
        agent_console=console,
    )

    assert exit_code == 0
    assert any("Spawned teammate code-reviewer" in status for status in console.statuses)
    assert any("Sent message" in status for status in console.statuses)
    assert any("code-reviewer [running] - code reviewer" in line for line in console.printed)


def test_run_repl_mode_command_uses_shared_input_reader(
    monkeypatch,
    tmp_path: Path,
) -> None:
    inputs = iter(["/mode", "2", "/exit"])
    read_calls: list[str] = []

    def _next_input(_session) -> str:
        read_calls.append("read")
        return next(inputs)

    monkeypatch.setattr("src.main._supports_prompt_toolkit", lambda: False)
    monkeypatch.setattr("src.main._read_user_input", _next_input)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": (_ for _ in ()).throw(AssertionError("builtins.input should not be used")),
    )

    console = DummyConsole()

    exit_code = run_repl(
        _make_config(),
        ReplayProvider([]),
        workspace=tmp_path,
        agent_console=console,
    )

    assert exit_code == 0
    assert read_calls == ["read", "read", "read"]
    assert any("Select [1-4] on the next prompt." == status for status in console.statuses)
    assert any("Permission mode: default → auto" == status for status in console.statuses)


def test_make_teammate_provider_factory_inherits_custom_api_key_env(monkeypatch) -> None:
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
    )

    factory = main_module._make_teammate_provider_factory(config)
    provider = factory({})

    assert provider == "provider"
    assert captured["config"].provider.api_key_env == "MY_OPENAI_KEY"  # type: ignore[index, union-attr]


def _make_config() -> Config:
    return Config(
        provider=ProviderConfig(
            name="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        ),
        permission=PermissionConfig(mode="default", allow=[], deny=[]),
        ui=UIConfig(stream=False, theme="dark"),
        subagent=SubagentConfig(max_depth=3, default_type="general-purpose"),
        thinking=ThinkingConfig(),
        path=Path("config.toml"),
    )


def test_nag_reminder_skips_tool_result_messages() -> None:
    """Bug #14: nag reminder should not insert between assistant and tool_result."""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "calling tool"},
            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"},
        ]},
    ]
    _refresh_nag_reminder(messages, "Remember to be concise.")

    # The nag should be inserted after the real user message, not the tool_result
    nag_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "system" and isinstance(m.get("content"), str)
        and "<nag-reminder>" in str(m["content"])
    ]
    assert len(nag_indices) == 1
    nag_idx = nag_indices[0]
    # The message before the nag should be the real user message
    assert messages[nag_idx - 1].get("content") == "do something"
    # The tool_result should still directly follow the assistant message
    assistant_idx = next(
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant"
    )
    tool_result_msg = messages[assistant_idx + 1]
    assert tool_result_msg.get("role") == "user"
    assert _is_tool_result_message(tool_result_msg)
