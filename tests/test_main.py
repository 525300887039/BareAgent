from pathlib import Path

from src.main import (
    DEFAULT_CONFIG_PATH,
    Config,
    PermissionConfig,
    ProviderConfig,
    UIConfig,
    load_config,
    resolve_config_path,
    run_repl,
)
from src.memory.transcript import TranscriptManager
from src.provider.base import BaseLLMProvider, LLMResponse, ThinkingConfig


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


def _make_config() -> Config:
    return Config(
        provider=ProviderConfig(
            name="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        ),
        permission=PermissionConfig(mode="default", allow=[], deny=[]),
        ui=UIConfig(stream=False, theme="dark"),
        thinking=ThinkingConfig(),
        path=Path("config.toml"),
    )
