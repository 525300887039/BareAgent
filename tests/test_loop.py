from __future__ import annotations

from copy import deepcopy

from src.core.loop import agent_loop
from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall


class MockProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = kwargs
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        return self._responses.pop(0)

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def test_agent_loop_returns_text_without_tool_calls() -> None:
    provider = MockProvider(
        [
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=12,
                output_tokens=4,
            )
        ]
    )
    messages = [
        {"role": "system", "content": "You are BareAgent."},
        {"role": "user", "content": "Say hi."},
    ]

    result = agent_loop(provider=provider, messages=messages, tools=[], handlers={})

    assert result == "Done."
    assert len(provider.calls) == 1
    assert messages[-1] == {"role": "assistant", "content": "Done."}


def test_agent_loop_executes_tool_calls_then_returns_text() -> None:
    provider = MockProvider(
        [
            LLMResponse(
                text="Checking file.",
                tool_calls=[
                    ToolCall(
                        id="toolu_1",
                        name="echo",
                        input={"value": "hello"},
                    )
                ],
                stop_reason="tool_use",
                input_tokens=20,
                output_tokens=10,
            ),
            LLMResponse(
                text="Tool finished.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=4,
            ),
        ]
    )
    messages = [
        {"role": "system", "content": "You are BareAgent."},
        {"role": "user", "content": "Run the tool."},
    ]
    tools = [
        {
            "name": "echo",
            "description": "Echo a value back.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
    ]

    result = agent_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        handlers={"echo": lambda value: f"handled {value}"},
    )

    assert result == "Tool finished."
    assert len(provider.calls) == 2
    assert provider.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking file."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "echo",
                "input": {"value": "hello"},
            },
        ],
    }
    assert provider.calls[1]["messages"][-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "handled hello",
            }
        ],
    }
