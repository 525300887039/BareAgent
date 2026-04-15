from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.core.loop import LLMCallError, agent_loop
from src.debug.interaction_log import InteractionLogger
from src.permission.guard import PermissionGuard, PermissionMode
from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_log_request_writes_expected_file_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: 100.5)
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "echo", "parameters": {"type": "object"}}]

    seq = logger.log_request(messages, tools, provider_info={"model": "gpt-test"})

    assert seq == 0
    payload = _load_json(tmp_path / ".logs" / "sess-1" / "000_request.json")
    assert payload == {
        "seq": 0,
        "type": "request",
        "timestamp": 100.5,
        "provider": {"model": "gpt-test"},
        "messages": messages,
        "tools": tools,
        "message_count": 1,
        "tool_count": 1,
    }


def test_log_response_writes_expected_file_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([10.0, 11.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    seq = logger.log_request([], [])
    logger.log_response(
        seq,
        text="done",
        thinking="analysis",
        tool_calls=[{"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}],
        input_tokens=12,
        output_tokens=5,
        duration_ms=123.456,
    )

    payload = _load_json(tmp_path / ".logs" / "sess-1" / "000_response.json")
    assert payload == {
        "seq": 0,
        "type": "response",
        "timestamp": 11.0,
        "text": "done",
        "thinking": "analysis",
        "tool_calls": [{"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}],
        "input_tokens": 12,
        "output_tokens": 5,
        "duration_ms": 123.46,
    }


def test_sequence_auto_increments_after_each_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    first_seq = logger.log_request([], [])
    logger.log_response(first_seq)
    second_seq = logger.log_request([], [])
    logger.log_response(second_seq)

    assert first_seq == 0
    assert second_seq == 1
    assert (tmp_path / ".logs" / "sess-1" / "000_request.json").is_file()
    assert (tmp_path / ".logs" / "sess-1" / "001_request.json").is_file()


def test_response_write_failure_still_advances_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    original_write = logger._write

    seq = logger.log_request([{"role": "user", "content": "first"}], [])

    def _broken_write(filename: str, payload: dict[str, Any]) -> None:
        if filename.endswith("_response.json"):
            raise OSError("disk full")
        original_write(filename, payload)

    monkeypatch.setattr(logger, "_write", _broken_write)
    with pytest.raises(OSError, match="disk full"):
        logger.log_response(seq, text="failed")

    monkeypatch.setattr(logger, "_write", original_write)
    next_seq = logger.log_request([{"role": "user", "content": "second"}], [])

    assert next_seq == 1
    assert _load_json(tmp_path / ".logs" / "sess-1" / "000_request.json")[
        "messages"
    ] == [{"role": "user", "content": "first"}]
    assert _load_json(tmp_path / ".logs" / "sess-1" / "001_request.json")[
        "messages"
    ] == [{"role": "user", "content": "second"}]


def test_list_sessions_returns_sorted_session_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))

    logger_a = InteractionLogger(log_dir=tmp_path / ".logs", session_id="beta")
    logger_b = InteractionLogger(log_dir=tmp_path / ".logs", session_id="alpha")
    logger_a.log_request([], [])
    logger_b.log_request([], [])

    assert logger_a.list_sessions() == ["alpha", "beta"]


def test_list_interactions_returns_expected_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([100.0, 101.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    seq = logger.log_request(
        [{"role": "user", "content": "hi"}],
        [{"name": "echo"}],
    )
    logger.log_response(
        seq,
        tool_calls=[{"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}],
        input_tokens=7,
        output_tokens=9,
        duration_ms=45.0,
    )

    assert logger.list_interactions("sess-1") == [
        {
            "seq": 0,
            "timestamp": 100.0,
            "message_count": 1,
            "tool_count": 1,
            "input_tokens": 7,
            "output_tokens": 9,
            "duration_ms": 45.0,
            "tool_call_count": 1,
            "has_error": False,
        }
    ]


def test_get_interaction_returns_full_request_and_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([20.0, 21.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    seq = logger.log_request([{"role": "user", "content": "hi"}], [])
    logger.log_response(seq, text="done")

    interaction = logger.get_interaction("sess-1", 0)

    assert interaction == {
        "seq": 0,
        "request": _load_json(tmp_path / ".logs" / "sess-1" / "000_request.json"),
        "response": _load_json(tmp_path / ".logs" / "sess-1" / "000_response.json"),
    }


def test_session_id_setter_resets_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="first")

    seq = logger.log_request([], [])
    logger.log_response(seq)
    logger.session_id = "second"
    next_seq = logger.log_request([], [])

    assert next_seq == 0
    assert (tmp_path / ".logs" / "first" / "000_request.json").is_file()
    assert (tmp_path / ".logs" / "second" / "000_request.json").is_file()


def test_existing_session_reuses_next_available_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    session_dir = tmp_path / ".logs" / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "000_request.json").write_text("{}", encoding="utf-8")
    (session_dir / "000_response.json").write_text("{}", encoding="utf-8")
    (session_dir / "001_request.json").write_text("{}", encoding="utf-8")

    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    seq = logger.log_request([], [])

    assert seq == 2
    assert (session_dir / "002_request.json").is_file()


def test_event_queue_receives_request_and_response_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.5, 2.5])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    event_queue = logger.event_queue

    seq = logger.log_request([], [])
    logger.log_response(seq)

    assert event_queue.get_nowait() == {
        "event": "request",
        "session_id": "sess-1",
        "seq": 0,
        "timestamp": 1.5,
    }
    assert event_queue.get_nowait() == {
        "event": "response",
        "session_id": "sess-1",
        "seq": 0,
        "timestamp": 2.5,
    }


def test_subscribe_events_uses_bounded_queue_and_drops_oldest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    event_queue = logger.subscribe_events(maxsize=2)

    seq = logger.log_request([], [])
    logger.log_response(seq)
    logger.log_request([], [])

    assert event_queue.get_nowait() == {
        "event": "response",
        "session_id": "sess-1",
        "seq": 0,
        "timestamp": 2.0,
    }
    assert event_queue.get_nowait() == {
        "event": "request",
        "session_id": "sess-1",
        "seq": 1,
        "timestamp": 3.0,
    }


def test_error_response_includes_error_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([10.0, 11.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    seq = logger.log_request([], [])
    logger.log_response(seq, error="boom", duration_ms=9.5)

    payload = _load_json(tmp_path / ".logs" / "sess-1" / "000_response.json")
    assert payload["error"] == "boom"
    assert payload["duration_ms"] == 9.5


def test_list_interactions_sorts_sequences_numerically(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / ".logs" / "sess-1"
    session_dir.mkdir(parents=True)
    for seq in (998, 999, 1000):
        (session_dir / f"{seq:03d}_request.json").write_text(
            json.dumps(
                {
                    "seq": seq,
                    "timestamp": float(seq),
                    "message_count": 1,
                    "tool_count": 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")

    assert [entry["seq"] for entry in logger.list_interactions("sess-1")] == [
        998,
        999,
        1000,
    ]


@pytest.mark.parametrize(
    "session_id",
    [
        "../secret",
        "..\\secret",
        "/tmp/secret",
        "C:\\secret",
    ],
)
def test_invalid_session_ids_are_rejected(
    tmp_path: Path,
    session_id: str,
) -> None:
    with pytest.raises(ValueError):
        InteractionLogger(log_dir=tmp_path / ".logs", session_id=session_id)

    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="safe-session")

    with pytest.raises(ValueError):
        logger.list_interactions(session_id)

    with pytest.raises(ValueError):
        logger.get_interaction(session_id, 0)

    with pytest.raises(ValueError):
        logger.session_id = session_id


class _SuccessfulProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.model = "test-model"
        self._calls = 0

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="Checking.",
                thinking="reasoning",
                tool_calls=[ToolCall(id="toolu_1", name="echo", input={"value": "hi"})],
                stop_reason="tool_use",
                input_tokens=11,
                output_tokens=7,
            )
        return LLMResponse(
            text="Done.",
            thinking="",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=13,
            output_tokens=5,
        )

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class _FailingProvider(BaseLLMProvider):
    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        raise RuntimeError("provider exploded")

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class _InterruptingProvider(BaseLLMProvider):
    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        raise KeyboardInterrupt("stopped")

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class _BrokenRequestLogger:
    def log_request(self, messages, tools, *, provider_info=None) -> int:
        _ = messages, tools, provider_info
        raise OSError("disk full")

    def log_response(self, seq, **kwargs) -> None:
        _ = seq, kwargs
        raise AssertionError(
            "log_response should not be called after request logging fails"
        )


class _BrokenResponseLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def log_request(self, messages, tools, *, provider_info=None) -> int:
        _ = messages, tools, provider_info
        return 0

    def log_response(self, seq, **kwargs) -> None:
        self.calls.append((seq, dict(kwargs)))
        raise OSError("disk full")


def test_agent_loop_logs_request_and_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0, 4.0])
    monotonic_values = iter([10.0, 10.125, 20.0, 20.05])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    monkeypatch.setattr("src.core.loop.time.monotonic", lambda: next(monotonic_values))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="loop")

    result = agent_loop(
        provider=_SuccessfulProvider(),
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo"}],
        handlers={"echo": lambda value: value},
        permission=PermissionGuard(PermissionMode.BYPASS),
        interaction_logger=logger,
    )

    assert result == "Done."
    request_payload = _load_json(tmp_path / ".logs" / "loop" / "000_request.json")
    first_response_payload = _load_json(
        tmp_path / ".logs" / "loop" / "000_response.json"
    )
    second_response_payload = _load_json(
        tmp_path / ".logs" / "loop" / "001_response.json"
    )
    assert request_payload["provider"] == {
        "provider_type": "_SuccessfulProvider",
        "model": "test-model",
    }
    assert first_response_payload["text"] == "Checking."
    assert first_response_payload["thinking"] == "reasoning"
    assert first_response_payload["tool_calls"] == [
        {"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}
    ]
    assert first_response_payload["input_tokens"] == 11
    assert first_response_payload["output_tokens"] == 7
    assert first_response_payload["duration_ms"] == 125.0
    assert second_response_payload["text"] == "Done."
    assert second_response_payload["tool_calls"] == []
    assert second_response_payload["duration_ms"] == 50.0


def test_agent_loop_logs_error_response_when_provider_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0])
    monotonic_values = iter([20.0, 20.05])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    monkeypatch.setattr("src.core.loop.time.monotonic", lambda: next(monotonic_values))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="loop")

    with pytest.raises(LLMCallError, match="RuntimeError: provider exploded"):
        agent_loop(
            provider=_FailingProvider(),
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            handlers={},
            interaction_logger=logger,
        )

    response_payload = _load_json(tmp_path / ".logs" / "loop" / "000_response.json")
    assert response_payload["error"] == "provider exploded"
    assert response_payload["duration_ms"] == 50.0


def test_agent_loop_logs_keyboard_interrupt_and_advances_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0, 3.0])
    monotonic_values = iter([20.0, 20.05])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    monkeypatch.setattr("src.core.loop.time.monotonic", lambda: next(monotonic_values))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="loop")

    with pytest.raises(KeyboardInterrupt, match="stopped"):
        agent_loop(
            provider=_InterruptingProvider(),
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            handlers={},
            interaction_logger=logger,
        )

    response_payload = _load_json(tmp_path / ".logs" / "loop" / "000_response.json")
    assert response_payload["error"] == "stopped"
    assert response_payload["duration_ms"] == 50.0

    next_seq = logger.log_request([], [])
    assert next_seq == 1


def test_agent_loop_continues_when_request_logging_fails() -> None:
    result = agent_loop(
        provider=_SuccessfulProvider(),
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo"}],
        handlers={"echo": lambda value: value},
        permission=PermissionGuard(PermissionMode.BYPASS),
        interaction_logger=_BrokenRequestLogger(),
    )

    assert result == "Done."


def test_agent_loop_continues_when_response_logging_fails() -> None:
    logger = _BrokenResponseLogger()

    result = agent_loop(
        provider=_SuccessfulProvider(),
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo"}],
        handlers={"echo": lambda value: value},
        permission=PermissionGuard(PermissionMode.BYPASS),
        interaction_logger=logger,
    )

    assert result == "Done."
    assert len(logger.calls) == 2


def test_agent_loop_preserves_provider_failure_when_error_logging_fails() -> None:
    logger = _BrokenResponseLogger()

    with pytest.raises(LLMCallError, match="RuntimeError: provider exploded"):
        agent_loop(
            provider=_FailingProvider(),
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            handlers={},
            interaction_logger=logger,
        )

    assert logger.calls == [
        (
            0,
            {
                "duration_ms": pytest.approx(logger.calls[0][1]["duration_ms"]),
                "error": "provider exploded",
            },
        )
    ]
