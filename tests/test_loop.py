from __future__ import annotations

import logging
from collections.abc import Generator
from copy import deepcopy
from typing import Any

import pytest

from bareagent.core.loop import LLMCallError, agent_loop
from bareagent.permission.guard import PermissionGuard, PermissionMode
from bareagent.provider.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall


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
            yield from events
            return response

        return _generator()


class FakeConsole:
    def __init__(self) -> None:
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

    def get_stream_printer(self) -> FakeStreamPrinter:
        return FakeStreamPrinter()


class LegacyConsole:
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
    instances: list[FakeStreamPrinter] = []

    def __init__(self, *args, **kwargs) -> None:
        _ = kwargs
        self.args = args
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


class ResettingStreamPrinter:
    def __init__(self) -> None:
        self._active = False
        self._chunks: list[str] = []

    def start(self) -> None:
        self._active = True

    def feed(self, token: str) -> None:
        if not self._active:
            self.start()
        self._chunks.append(token)

    def finish(self) -> str:
        result = "".join(self._chunks)
        self._chunks = []
        self._active = False
        return result


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
        permission=PermissionGuard(PermissionMode.BYPASS),
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
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

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
        permission=PermissionGuard(PermissionMode.BYPASS),
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


def test_agent_loop_treats_pre_tool_stream_text_as_streamed_output() -> None:
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
                [StreamEvent(type="text", text="Done.")],
                LLMResponse(
                    text="Done.",
                    tool_calls=[],
                    stop_reason="end_turn",
                    input_tokens=8,
                    output_tokens=3,
                ),
            ),
        ],
    )
    console = FakeConsole()
    console.get_stream_printer = lambda: ResettingStreamPrinter()  # type: ignore[method-assign]

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Run the tool."},
        ],
        tools=[],
        handlers={"echo": lambda value: f"handled {value}"},
        permission=PermissionGuard(PermissionMode.BYPASS),
        stream=True,
        console=console,
    )

    assert result == "Done."
    assert console.assistant == []


def test_agent_loop_falls_back_to_non_stream_mode(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

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


def test_agent_loop_streams_with_legacy_console_shape(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

    provider = MockProvider(
        [],
        stream_payloads=[
            (
                [StreamEvent(type="text", text="Legacy stream.")],
                LLMResponse(
                    text="Legacy stream.",
                    tool_calls=[],
                    stop_reason="end_turn",
                    input_tokens=8,
                    output_tokens=3,
                ),
            ),
        ],
    )
    console = LegacyConsole()

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Say hi."},
        ],
        tools=[],
        handlers={},
        stream=True,
        console=console,  # type: ignore[arg-type]
    )

    assert result == "Legacy stream."
    assert len(provider.stream_calls) == 1
    assert console.assistant == []
    assert [instance.chunks for instance in FakeStreamPrinter.instances] == [["Legacy stream."]]
    assert FakeStreamPrinter.instances[0].args == (console.console,)


def test_agent_loop_does_not_fall_back_for_stream_runtime_error(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

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
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

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


class _RecordingTracker:
    def __init__(self) -> None:
        self.records: list[tuple[Any, str]] = []

    def record(self, response: Any, model: str) -> None:
        self.records.append((response, model))


def test_agent_loop_records_token_usage_non_stream() -> None:
    provider = MockProvider(
        [
            LLMResponse(
                text="Checking file.",
                tool_calls=[ToolCall(id="toolu_1", name="echo", input={"value": "hi"})],
                stop_reason="tool_use",
                input_tokens=20,
                output_tokens=10,
            ),
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=4,
            ),
        ]
    )
    provider.model = "test-model"  # type: ignore[attr-defined]
    tracker = _RecordingTracker()

    agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Run the tool."},
        ],
        tools=[],
        handlers={"echo": lambda value: f"handled {value}"},
        permission=PermissionGuard(PermissionMode.BYPASS),
        token_tracker=tracker,
    )

    # Both LLM round-trips inside the single user turn are recorded.
    assert [model for _, model in tracker.records] == ["test-model", "test-model"]
    assert [resp.input_tokens for resp, _ in tracker.records] == [20, 10]


def test_agent_loop_records_token_usage_stream(monkeypatch) -> None:
    FakeStreamPrinter.instances.clear()
    monkeypatch.setattr("bareagent.core.loop.StreamPrinter", FakeStreamPrinter)

    provider = MockProvider(
        [],
        stream_payloads=[
            (
                [StreamEvent(type="text", text="Streamed.")],
                LLMResponse(
                    text="Streamed.",
                    tool_calls=[],
                    stop_reason="end_turn",
                    input_tokens=8,
                    output_tokens=3,
                ),
            ),
        ],
    )
    provider.model = "stream-model"  # type: ignore[attr-defined]
    tracker = _RecordingTracker()

    agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Say hi."},
        ],
        tools=[],
        handlers={},
        stream=True,
        console=FakeConsole(),
        token_tracker=tracker,
    )

    assert len(tracker.records) == 1
    response, model = tracker.records[0]
    assert model == "stream-model"
    assert (response.input_tokens, response.output_tokens) == (8, 3)


def test_agent_loop_without_tracker_does_not_crash() -> None:
    provider = MockProvider(
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

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "You are BareAgent."},
            {"role": "user", "content": "Say hi."},
        ],
        tools=[],
        handlers={},
    )

    assert result == "Done."


def test_agent_loop_terminates_after_max_iterations() -> None:
    """Bug #12: agent_loop should stop after max_iterations even if LLM keeps
    returning tool calls."""
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
            permission=PermissionGuard(PermissionMode.BYPASS),
            console=console,
            max_iterations=3,
        )

    assert len(provider.calls) == 3
    assert any("exceeded 3 iterations" in e for e in console.errors)


# --- hook_engine integration ---------------------------------------------


class _HookOutcome:
    def __init__(self, block: bool = False, reason: str = "") -> None:
        self.block = block
        self.reason = reason


class FakeHookEngine:
    """Records hook invocations; PreToolUse can be configured to block."""

    def __init__(self, *, pre_block: _HookOutcome | None = None) -> None:
        self.pre_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, object, bool]] = []
        self._pre_block = pre_block or _HookOutcome()

    def run_pre_tool_use(self, tool_name, tool_input, *, session_id, cwd):
        self.pre_calls.append((tool_name, tool_input))
        return self._pre_block

    def run_post_tool_use(self, tool_name, tool_input, tool_output, *, is_error, session_id, cwd):
        self.post_calls.append((tool_name, tool_input, tool_output, is_error))


def _single_tool_then_done() -> MockProvider:
    return MockProvider(
        [
            LLMResponse(
                text="Calling.",
                tool_calls=[ToolCall(id="toolu_1", name="echo", input={"value": "hi"})],
                stop_reason="tool_use",
                input_tokens=10,
                output_tokens=5,
            ),
            LLMResponse(
                text="Done.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=5,
                output_tokens=2,
            ),
        ]
    )


def test_pre_tool_use_block_skips_handler_and_returns_error_result() -> None:
    provider = _single_tool_then_done()
    called: list[str] = []

    def handler(value: str) -> str:
        called.append(value)
        return f"handled {value}"

    engine = FakeHookEngine(pre_block=_HookOutcome(block=True, reason="nope"))

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        tools=[],
        handlers={"echo": handler},
        permission=PermissionGuard(PermissionMode.BYPASS),
        hook_engine=engine,
    )

    assert result == "Done."
    assert called == []  # handler never ran
    assert engine.post_calls == []  # PostToolUse not fired on block
    # The error result was fed back to the LLM on the follow-up turn.
    tool_result = provider.calls[1]["messages"][-1]["content"][0]
    assert tool_result["content"] == "nope"
    assert tool_result["is_error"] is True


def test_pre_tool_use_allow_runs_handler_then_post_hook() -> None:
    provider = _single_tool_then_done()
    engine = FakeHookEngine()

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        tools=[],
        handlers={"echo": lambda value: f"handled {value}"},
        permission=PermissionGuard(PermissionMode.BYPASS),
        hook_engine=engine,
    )

    assert result == "Done."
    assert engine.pre_calls == [("echo", {"value": "hi"})]
    assert engine.post_calls == [("echo", {"value": "hi"}, "handled hi", False)]


def test_post_hook_not_fired_when_handler_raises() -> None:
    provider = _single_tool_then_done()
    engine = FakeHookEngine()

    def boom(value: str) -> str:
        raise RuntimeError("kaboom")

    agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        tools=[],
        handlers={"echo": boom},
        permission=PermissionGuard(PermissionMode.BYPASS),
        hook_engine=engine,
    )

    assert engine.pre_calls == [("echo", {"value": "hi"})]
    assert engine.post_calls == []  # PostToolUse skipped on handler failure


def test_no_hook_engine_preserves_existing_behavior() -> None:
    provider = _single_tool_then_done()

    result = agent_loop(
        provider=provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        tools=[],
        handlers={"echo": lambda value: f"handled {value}"},
        permission=PermissionGuard(PermissionMode.BYPASS),
    )

    assert result == "Done."
    tool_result = provider.calls[1]["messages"][-1]["content"][0]
    assert tool_result["content"] == "handled hi"
    assert "is_error" not in tool_result


# --- task 06-08-provider-empty-response-diagnostic ------------------------


def _empty_response() -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[],
        stop_reason="completed",
        input_tokens=4404,
        output_tokens=5,
    )


def test_agent_loop_warns_on_empty_response(caplog) -> None:
    """A normal stop with no text and no tool calls fires a non-fatal diagnostic."""
    provider = MockProvider([_empty_response()])
    console = FakeConsole()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]

    with caplog.at_level(logging.WARNING, logger="bareagent.core.loop"):
        result = agent_loop(
            provider=provider, messages=messages, tools=[], handlers={}, console=console
        )

    # Control flow unchanged: still returns "" and appends the assistant turn.
    assert result == ""
    # Diagnostic surfaced on both channels with stop_reason + output_tokens.
    assert len(console.statuses) == 1
    assert "empty response" in console.statuses[0]
    assert "completed" in console.statuses[0]
    assert "output_tokens=5" in console.statuses[0]
    assert console.errors == []  # warning, not an error
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("empty response" in r.getMessage() for r in warnings)


def test_agent_loop_no_warning_on_normal_text(caplog) -> None:
    provider = MockProvider(
        [
            LLMResponse(
                text="Hello.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=5,
                output_tokens=2,
            )
        ]
    )
    console = FakeConsole()
    with caplog.at_level(logging.WARNING, logger="bareagent.core.loop"):
        result = agent_loop(
            provider=provider,
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            handlers={},
            console=console,
        )

    assert result == "Hello."
    assert console.statuses == []
    assert not [r for r in caplog.records if "empty response" in r.getMessage()]


def test_agent_loop_empty_response_without_console_still_logs(caplog) -> None:
    """Console-less paths (sub-agents/teammates) still leave a logged trace."""
    provider = MockProvider([_empty_response()])
    with caplog.at_level(logging.WARNING, logger="bareagent.core.loop"):
        result = agent_loop(
            provider=provider,
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            handlers={},
        )

    assert result == ""
    assert any("empty response" in r.getMessage() for r in caplog.records)


def test_agent_loop_no_warning_on_tool_only_turn(caplog) -> None:
    """An empty-text turn that carries tool calls is normal -- no diagnostic."""
    provider = MockProvider(
        [
            LLMResponse(
                text="",
                tool_calls=[ToolCall(id="t1", name="echo", input={"value": "hi"})],
                stop_reason="tool_use",
                input_tokens=5,
                output_tokens=3,
            ),
            LLMResponse(
                text="Done.", tool_calls=[], stop_reason="end_turn", input_tokens=5, output_tokens=2
            ),
        ]
    )
    console = FakeConsole()
    with caplog.at_level(logging.WARNING, logger="bareagent.core.loop"):
        result = agent_loop(
            provider=provider,
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            handlers={"echo": lambda value: f"handled {value}"},
            permission=PermissionGuard(PermissionMode.BYPASS),
            console=console,
        )

    assert result == "Done."
    assert console.statuses == []
    assert not [r for r in caplog.records if "empty response" in r.getMessage()]
