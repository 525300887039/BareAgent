from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.debug.interaction_log import InteractionLogger
from src.tracing.json_file import JsonFileSpan, JsonFileTracer


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_json_file_tracer_delegates_log_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: 100.0)
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    seq = tracer.log_request(
        [{"role": "user", "content": "hi"}],
        [{"name": "echo"}],
        provider_info={"model": "test"},
    )
    assert seq == 0
    payload = _load_json(tmp_path / ".logs" / "sess-1" / "000_request.json")
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["provider"] == {"model": "test"}


def test_json_file_tracer_delegates_log_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([10.0, 11.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    seq = tracer.log_request([], [])
    tracer.log_response(seq, text="done", input_tokens=5, output_tokens=3)

    payload = _load_json(tmp_path / ".logs" / "sess-1" / "000_response.json")
    assert payload["text"] == "done"
    assert payload["input_tokens"] == 5


def test_json_file_tracer_exposes_logger_for_web_viewer(
    tmp_path: Path,
) -> None:
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)
    assert tracer.logger is logger


def test_json_file_tracer_session_id_sync(
    tmp_path: Path,
) -> None:
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="first")
    tracer = JsonFileTracer(logger)

    assert tracer.session_id == "first"
    tracer.session_id = "second"
    assert logger.session_id == "second"
    assert tracer.session_id == "second"


def test_json_file_tracer_trace_yields_span(
    tmp_path: Path,
) -> None:
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    with tracer.trace("llm_call", tags={"model": "test"}) as span:
        assert isinstance(span, JsonFileSpan)
        assert tracer.current_span() is span
        span.set_tag("input_tokens", 10)

    assert tracer.current_span() is None


def test_json_file_tracer_trace_captures_error(
    tmp_path: Path,
) -> None:
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    with pytest.raises(ValueError, match="boom"):
        with tracer.trace("llm_call") as span:
            raise ValueError("boom")

    assert span._error == "boom"


def test_json_file_tracer_list_and_get_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    seq = tracer.log_request([{"role": "user", "content": "hi"}], [])
    tracer.log_response(seq, text="ok")

    assert "sess-1" in tracer.list_sessions()
    interactions = tracer.list_interactions("sess-1")
    assert len(interactions) == 1
    detail = tracer.get_interaction("sess-1", 0)
    assert detail["request"] is not None
    assert detail["response"] is not None


def test_json_file_tracer_subscribe_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1.0, 2.0])
    monkeypatch.setattr("src.debug.interaction_log.time.time", lambda: next(timestamps))
    logger = InteractionLogger(log_dir=tmp_path / ".logs", session_id="sess-1")
    tracer = JsonFileTracer(logger)

    event_queue = tracer.subscribe_events()
    tracer.log_request([], [])

    event = event_queue.get_nowait()
    assert event["event"] == "request"

    tracer.unsubscribe_events(event_queue)
