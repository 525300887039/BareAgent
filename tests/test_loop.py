from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from typing import Any

import pytest

from src.core.loop import agent_loop, LLMCallError
from src.provider.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall


class MockProvider(BaseLLMProvider):
    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        stream_payloads: list[
            tuple[list[StreamEvent], LLMResponse] | Exception | Generator[StreamEvent, None, Any]
        ]
        | None = None,
    ) -> None:
        self._responses = list(responses)
        self._stream_payloads = list(stream_payloads or [])
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = kwargs
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        return self._responses.pop(0)

    def create_stream(self, messages, tools, **kwargs):
        _ = kwargs
        self.stream_calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        payload = self._stream_payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if not isinstance(payload, tuple):
            return payload
        events, response = payload

        def _generator():
            for event in events:
                yield event
            return response

        return _generator()


class FakeConsole:
    def __init__(self) -> None:
        self.console = type("ConsoleProxy", (), {"print": lambda *args, **kwargs: None})()
        self.assistant: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def print_assistant(self, text: str) -> None:
        self.assistant.append(text)

    def print_tool_call(self, name: str, input_data: dict) -> None:
        self.tool_calls.append((name, input_data))

    def print_tool_result(self, name: str, output) -> None:
        self.tool_results.append((name, str(output)))

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_error(self, msg: str) -> None:
        self.errors.append(msg)


class FakeStreamPrinter:
    instances: list["FakeStreamPrinter"] = []

    def __init__(self, *args, **kwargs) -> None:
        _ = args, kwargs
        self.started = False
        self.chunks: list[str] = []
        FakeStreamPrinter.instances.append(self)

    def start(self) -> None:
        self.started = True

    def feed(self, token: str) -> None:
        self.chunks.append(token)

    def finish(self) -> str:
        self.started = False
        return "".join(self.chunks)


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


def test_agent_loop_streams_and_formats_tool_activity(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("src.core.loop.StreamPrinter", FakeStreamPrinter)

    provider = MockProvider(
        [],
        stream_payloads=[
            (
                [
                    StreamEvent(type="text", text="Checking file."),
                    StreamEvent(
                        type="tool_call",
                        tool_call_id="toolu_1",
                        name="echo",
                        input={"value": "hello"},
                    ),
                ],
                LLMResponse(
                    text="Checking file.",
                    tool_calls=[ToolCall(id="toolu_1", name="echo", input={"value": "hello"})],
                    stop_reason="tool_use",
                    input_tokens=10,
                    output_tokens=5,
                ),
            ),
            (
                [StreamEvent(type="text", text="Tool finished.")],
                LLMResponse(
                    text="Tool finished.",
                    tool_calls=[],
                    stop_reason="end_turn",
                    input_tokens=8,
                    output_tokens=3,
                ),
            ),
        ],
    )
    console = FakeConsole()

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Run the tool."},
        ],
        tools=[],
        handlers={"echo": lambda value: f"handled {value}"},
        stream=True,
        console=console,
    )

    assert result == "Tool finished."
    assert len(provider.calls) == 0
    assert len(provider.stream_calls) == 2
    assert console.tool_calls == [("echo", {"value": "hello"})]
    assert console.tool_results == [("echo", "handled hello")]
    assert console.assistant == []
    assert [instance.chunks for instance in FakeStreamPrinter.instances] == [
        ["Checking file."],
        ["Tool finished."],
    ]


def test_agent_loop_falls_back_to_non_stream_mode(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("src.core.loop.StreamPrinter", FakeStreamPrinter)

    provider = MockProvider(
        [
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=2,
            )
        ],
        stream_payloads=[NotImplementedError("no streaming support")],
    )
    console = FakeConsole()

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Say hi."},
        ],
        tools=[],
        handlers={},
        stream=True,
        console=console,
    )

    assert result == "Done."
    assert len(provider.stream_calls) == 1
    assert len(provider.calls) == 1
    assert console.assistant == ["Done."]
    assert any("falling back to non-stream mode" in status for status in console.statuses)


def test_agent_loop_does_not_fall_back_for_stream_runtime_error(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("src.core.loop.StreamPrinter", FakeStreamPrinter)

    provider = MockProvider(
        [
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=2,
            )
        ],
        stream_payloads=[RuntimeError("connection reset")],
    )
    console = FakeConsole()

    with pytest.raises(LLMCallError, match="RuntimeError: connection reset"):
        agent_loop(
            provider=provider,
            messages=[
                {"role": "system", "content": "You are BareAgent."},
                {"role": "user", "content": "Say hi."},
            ],
            tools=[],
            handlers={},
            stream=True,
            console=console,
        )

    assert len(provider.stream_calls) == 1
    assert len(provider.calls) == 0
    assert console.errors == ["LLM call failed: RuntimeError: connection reset"]


def test_agent_loop_does_not_retry_after_partial_stream_failure(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("src.core.loop.StreamPrinter", FakeStreamPrinter)

    def _broken_stream():
        yield StreamEvent(type="text", text="Partial reply")
        raise RuntimeError("stream reset")

    provider = MockProvider(
        [],
        stream_payloads=[_broken_stream()],
    )
    console = FakeConsole()

    with pytest.raises(LLMCallError, match="RuntimeError: stream reset"):
        agent_loop(
            provider=provider,
            messages=[
                {"role": "system", "content": "You are BareAgent."},
                {"role": "user", "content": "Say hi."},
            ],
            tools=[],
            handlers={},
            stream=True,
            console=console,
        )

    assert len(provider.stream_calls) == 1
    assert len(provider.calls) == 0
    assert console.errors == ["LLM call failed: RuntimeError: stream reset"]
    assert [instance.chunks for instance in FakeStreamPrinter.instances] == [["Partial reply"]]


def test_agent_loop_terminates_after_max_iterations() -> None:
    """Bug #12: agent_loop should stop after max_iterations even if LLM keeps returning tool calls."""
    tool_response = LLMResponse(
        text="Calling tool.",
        tool_calls=[ToolCall(id="toolu_1", name="echo", input={"value": "hi"})],
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=5,
    )
    provider = MockProvider([tool_response, tool_response, tool_response, tool_response])
    console = FakeConsole()

    with pytest.raises(LLMCallError, match="exceeded 3 iterations"):
        agent_loop(
            provider=provider,
            messages=[
                {"role": "system", "content": "You are BareAgent."},
                {"role": "user", "content": "Loop forever."},
            ],
            tools=[],
            handlers={"echo": lambda value: f"handled {value}"},
            console=console,
            max_iterations=3,
        )

    assert len(provider.calls) == 3
    assert any("exceeded 3 iterations" in e for e in console.errors)
