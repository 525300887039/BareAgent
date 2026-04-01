from __future__ import annotations

from types import SimpleNamespace

from src.provider import factory
from src.provider.anthropic import AnthropicProvider
from src.provider.base import LLMResponse, ThinkingConfig, ToolCall
from src.provider.openai import OpenAIProvider


def test_llm_response_has_tool_calls_and_to_message() -> None:
    response = LLMResponse(
        text="I will inspect the file.",
        tool_calls=[
            ToolCall(
                id="toolu_1",
                name="read_file",
                input={"path": "src/main.py"},
            )
        ],
        stop_reason="stop",
        input_tokens=11,
        output_tokens=7,
        thinking="Need to inspect the file first.",
    )

    assert response.has_tool_calls is True
    assert response.thinking == "Need to inspect the file first."
    assert response.to_message() == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect the file."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "src/main.py"},
            },
        ],
    }


def test_llm_response_without_tool_calls_uses_plain_text_message() -> None:
    response = LLMResponse(
        text="All done.",
        tool_calls=[],
        stop_reason="end_turn",
        input_tokens=8,
        output_tokens=3,
    )

    assert response.has_tool_calls is False
    assert response.to_message() == {"role": "assistant", "content": "All done."}


def test_thinking_config_defaults() -> None:
    config = ThinkingConfig()

    assert config.mode == "adaptive"
    assert config.budget_tokens == 10000


def test_anthropic_parse_response_extracts_thinking_and_tool_calls(monkeypatch) -> None:
    class FakeAnthropicClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.messages = SimpleNamespace()

    monkeypatch.setattr("src.provider.anthropic.anthropic.Anthropic", FakeAnthropicClient)
    provider = AnthropicProvider(api_key="test", model="claude-test")

    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="Need to inspect.",
                signature="sig_123",
            ),
            SimpleNamespace(type="text", text="Checking now."),
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="read_file",
                input={"path": "src/main.py"},
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=21, output_tokens=9),
    )

    parsed = provider._parse_response(response)

    assert parsed.text == "Checking now."
    assert parsed.thinking == "Need to inspect."
    assert parsed.tool_calls == [
        ToolCall(id="toolu_1", name="read_file", input={"path": "src/main.py"})
    ]
    assert parsed.to_message() == {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "Need to inspect.",
                "signature": "sig_123",
            },
            {"type": "text", "text": "Checking now."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "src/main.py"},
            },
        ],
    }


def test_openai_parse_response_extracts_tool_calls(monkeypatch) -> None:
    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="grep",
                                arguments='{"pattern":"TODO"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=13, completion_tokens=6),
    )

    parsed = provider._parse_response(response)

    assert parsed.has_tool_calls is True
    assert parsed.tool_calls == [
        ToolCall(id="call_1", name="grep", input={"pattern": "TODO"})
    ]
    assert parsed.input_tokens == 13
    assert parsed.output_tokens == 6


def test_factory_builds_anthropic_provider_with_thinking(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnthropicProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setattr(factory, "AnthropicProvider", FakeAnthropicProvider)

    config = SimpleNamespace(
        provider=SimpleNamespace(
            name="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
            base_url=None,
        ),
        thinking=SimpleNamespace(mode="enabled", budget_tokens=4096),
    )

    factory.create_provider(config)

    assert captured["api_key"] == "secret"
    assert captured["model"] == "claude-sonnet-4-20250514"
    assert captured["thinking_config"] == ThinkingConfig(
        mode="enabled",
        budget_tokens=4096,
    )


def test_factory_builds_deepseek_via_openai_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr(factory, "OpenAIProvider", FakeOpenAIProvider)

    config = SimpleNamespace(
        provider=SimpleNamespace(
            name="deepseek",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            base_url=None,
        ),
        thinking=ThinkingConfig(),
    )

    factory.create_provider(config)

    assert captured == {
        "api_key": "secret",
        "model": "deepseek-chat",
        "base_url": factory.DEEPSEEK_BASE_URL,
    }
