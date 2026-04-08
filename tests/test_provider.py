from __future__ import annotations

from types import SimpleNamespace

from src.provider import factory
from src.provider.anthropic import AnthropicProvider
from src.provider.base import LLMResponse, StreamEvent, ThinkingConfig, ToolCall
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


def test_anthropic_create_stream_yields_text_and_tool_events(monkeypatch) -> None:
    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Checking now."),
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="read_file",
                input={"file_path": "src/main.py"},
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=21, output_tokens=9),
    )

    class FakeStream:
        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text="Checking now."),
                    ),
                    SimpleNamespace(
                        type="content_block_stop",
                        content_block=SimpleNamespace(
                            type="tool_use",
                            id="toolu_1",
                            name="read_file",
                            input={"file_path": "src/main.py"},
                        ),
                    ),
                ]
            )

        def get_final_message(self):
            return final_message

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

    class FakeAnthropicClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.messages = SimpleNamespace(stream=lambda **kwargs: FakeStream())

    monkeypatch.setattr("src.provider.anthropic.anthropic.Anthropic", FakeAnthropicClient)
    provider = AnthropicProvider(api_key="test", model="claude-test")

    stream = provider.create_stream(
        messages=[{"role": "user", "content": "Read the file."}],
        tools=[],
    )
    events: list[StreamEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            response = stop.value
            break

    assert events == [
        StreamEvent(type="text", text="Checking now."),
        StreamEvent(
            type="tool_call",
            tool_call_id="toolu_1",
            name="read_file",
            input={"file_path": "src/main.py"},
        ),
    ]
    assert response.text == "Checking now."
    assert response.tool_calls == [
        ToolCall(id="toolu_1", name="read_file", input={"file_path": "src/main.py"})
    ]


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


def test_openai_create_stream_accumulates_text_and_tool_calls(monkeypatch) -> None:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(content="Check", tool_calls=None),
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(name="grep", arguments='{"pattern":"TO'),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='DO"}'),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=13, completion_tokens=6),
        ),
    ]

    class FakeChatCompletions:
        def create(self, **kwargs):
            _ = kwargs
            return iter(chunks)

    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.chat = SimpleNamespace(completions=FakeChatCompletions())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    stream = provider.create_stream(
        messages=[{"role": "user", "content": "Run grep."}],
        tools=[],
    )
    events: list[StreamEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            response = stop.value
            break

    assert events == [
        StreamEvent(type="text", text="Check"),
        StreamEvent(
            type="tool_call",
            tool_call_id="call_1",
            name="grep",
            input={"pattern": "TODO"},
        ),
    ]
    assert response.text == "Check"
    assert response.tool_calls == [
        ToolCall(id="call_1", name="grep", input={"pattern": "TODO"})
    ]
    assert response.input_tokens == 13
    assert response.output_tokens == 6


def test_openai_create_stream_emits_tool_calls_even_without_tool_finish_reason(monkeypatch) -> None:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(name="echo", arguments='{"value":"STREAM'),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='_TOOL"}'),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=4),
        ),
    ]

    class FakeChatCompletions:
        def create(self, **kwargs):
            _ = kwargs
            return iter(chunks)

    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.chat = SimpleNamespace(completions=FakeChatCompletions())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    stream = provider.create_stream(
        messages=[{"role": "user", "content": "Use the tool."}],
        tools=[],
    )
    events: list[StreamEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            response = stop.value
            break

    assert events == [
        StreamEvent(
            type="tool_call",
            tool_call_id="call_1",
            name="echo",
            input={"value": "STREAM_TOOL"},
        )
    ]
    assert response.tool_calls == [
        ToolCall(id="call_1", name="echo", input={"value": "STREAM_TOOL"})
    ]
    assert response.stop_reason == "tool_calls"


def test_openai_parse_responses_api_payload_extracts_tool_calls() -> None:
    provider = OpenAIProvider(api_key="test", model="gpt-test", wire_api="responses")
    response = "\n".join(
        [
            "event: response.completed",
            (
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"usage":{"input_tokens":13,"output_tokens":6},"output":['
                '{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Checking now."}]},'
                '{"type":"function_call","call_id":"call_1","name":"grep","arguments":"{\\"pattern\\": \\"TODO\\"}"}'
                "]}}"
            ),
            "",
        ]
    )

    parsed = provider._parse_responses_api_response(response)

    assert parsed.text == "Checking now."
    assert parsed.tool_calls == [
        ToolCall(id="call_1", name="grep", input={"pattern": "TODO"})
    ]
    assert parsed.input_tokens == 13
    assert parsed.output_tokens == 6


def test_openai_create_stream_via_responses_accumulates_text_and_tool_calls(monkeypatch) -> None:
    completed_response = SimpleNamespace(
        to_dict=lambda: {
            "status": "completed",
            "usage": {"input_tokens": 13, "output_tokens": 6},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Checking now."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "grep",
                    "arguments": '{"pattern":"TODO"}',
                },
            ],
        }
    )
    events_source = [
        SimpleNamespace(type="response.output_text.delta", delta="Checking ", item_id="msg_1"),
        SimpleNamespace(type="response.output_text.delta", delta="now.", item_id="msg_1"),
        SimpleNamespace(
            type="response.output_item.done",
            item=SimpleNamespace(
                type="function_call",
                call_id="call_1",
                id="fc_1",
                name="grep",
                arguments='{"pattern":"TODO"}',
            ),
        ),
        SimpleNamespace(type="response.completed", response=completed_response),
    ]

    class FakeResponsesAPI:
        def create(self, **kwargs):
            _ = kwargs
            return iter(events_source)

    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.responses = FakeResponsesAPI()
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="test", model="gpt-test", wire_api="responses")

    stream = provider.create_stream(
        messages=[{"role": "user", "content": "Run grep."}],
        tools=[],
    )
    events: list[StreamEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            response = stop.value
            break

    assert events == [
        StreamEvent(type="text", text="Checking "),
        StreamEvent(type="text", text="now."),
        StreamEvent(
            type="tool_call",
            tool_call_id="call_1",
            name="grep",
            input={"pattern": "TODO"},
        ),
    ]
    assert response.text == "Checking now."
    assert response.tool_calls == [
        ToolCall(id="call_1", name="grep", input={"pattern": "TODO"})
    ]
    assert response.input_tokens == 13
    assert response.output_tokens == 6


def test_openai_create_stream_via_responses_preserves_streamed_tool_calls_when_completed_payload_omits_them(
    monkeypatch,
) -> None:
    completed_response = SimpleNamespace(
        to_dict=lambda: {
            "status": "completed",
            "usage": {"input_tokens": 13, "output_tokens": 6},
            "output": [],
        }
    )
    events_source = [
        SimpleNamespace(type="response.output_text.delta", delta="Checking ", item_id="msg_1"),
        SimpleNamespace(type="response.output_text.delta", delta="now.", item_id="msg_1"),
        SimpleNamespace(
            type="response.output_item.done",
            item=SimpleNamespace(
                type="function_call",
                call_id="call_1",
                id="fc_1",
                name="grep",
                arguments='{"pattern":"TODO"}',
            ),
        ),
        SimpleNamespace(type="response.completed", response=completed_response),
    ]

    class FakeResponsesAPI:
        def create(self, **kwargs):
            _ = kwargs
            return iter(events_source)

    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.responses = FakeResponsesAPI()
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="test", model="gpt-test", wire_api="responses")

    stream = provider.create_stream(
        messages=[{"role": "user", "content": "Run grep."}],
        tools=[],
    )
    events: list[StreamEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            response = stop.value
            break

    assert events == [
        StreamEvent(type="text", text="Checking "),
        StreamEvent(type="text", text="now."),
        StreamEvent(
            type="tool_call",
            tool_call_id="call_1",
            name="grep",
            input={"pattern": "TODO"},
        ),
    ]
    assert response.text == "Checking now."
    assert response.tool_calls == [
        ToolCall(id="call_1", name="grep", input={"pattern": "TODO"})
    ]
    assert response.stop_reason == "tool_calls"
    assert response.to_message() == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking now."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "grep",
                "input": {"pattern": "TODO"},
            },
        ],
    }


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


def test_openai_convert_assistant_message_missing_id_and_name(monkeypatch) -> None:
    """BUG-03：tool_use 块缺少 id/name 时不应 KeyError。"""
    class FakeOpenAIClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    provider = OpenAIProvider(api_key="x", model="gpt-4")
    content = [{"type": "tool_use", "input": {}}]
    result = provider._convert_assistant_message(content)
    assert result["tool_calls"][0]["id"] == ""
    assert result["tool_calls"][0]["function"]["name"] == ""


def test_anthropic_convert_message_content_missing_id_and_name(monkeypatch) -> None:
    """BUG-10：tool_use 块缺少 id/name 时不应 KeyError。"""
    class FakeAnthropicClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.messages = SimpleNamespace()

    monkeypatch.setattr("src.provider.anthropic.anthropic.Anthropic", FakeAnthropicClient)
    provider = AnthropicProvider(api_key="x", model="claude-3-5-sonnet-20241022")
    content = [{"type": "tool_use", "input": {}}]
    result = provider._convert_message_content(content)
    assert result[0]["id"] == ""
    assert result[0]["name"] == ""


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
        "wire_api": None,
    }
