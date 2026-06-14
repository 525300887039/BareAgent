"""Tests for src.mcp._sse — WHATWG SSE parser."""

from __future__ import annotations

from bareagent.mcp._sse import parse_sse_stream


def _events(lines: list[str]) -> list[dict[str, object]]:
    return [dict(e) for e in parse_sse_stream(lines)]


def test_single_event_with_one_data_line() -> None:
    events = _events(["event: message", "data: hello", ""])
    assert events == [{"event": "message", "data": "hello", "id": "", "retry": None}]


def test_default_event_type_is_message() -> None:
    events = _events(["data: hi", ""])
    assert events[0]["event"] == "message"


def test_multi_line_data_joined_with_newline() -> None:
    events = _events(["event: message", 'data: {"a":1,', 'data: "b":2}', ""])
    assert events[0]["data"] == '{"a":1,\n"b":2}'


def test_comment_lines_are_ignored() -> None:
    events = _events([": keepalive", "data: payload", ""])
    assert events == [{"event": "message", "data": "payload", "id": "", "retry": None}]


def test_empty_data_does_not_dispatch() -> None:
    # Empty line with no accumulated data should not yield an event.
    assert _events(["", "event: foo", ""]) == []


def test_multiple_events_separated_by_blank_lines() -> None:
    lines = [
        "event: endpoint",
        "data: /messages?session=abc",
        "",
        "event: message",
        "data: {}",
        "",
    ]
    events = _events(lines)
    assert len(events) == 2
    assert events[0]["event"] == "endpoint"
    assert events[0]["data"] == "/messages?session=abc"
    assert events[1]["event"] == "message"
    assert events[1]["data"] == "{}"


def test_bom_stripped_only_on_first_line() -> None:
    events = _events(["﻿data: x", ""])
    assert events[0]["data"] == "x"


def test_id_field_updates_last_event_id() -> None:
    events = _events(["data: x", "id: 42", "", "data: y", ""])
    assert events[0]["id"] == "42"
    # last-event-id persists across events
    assert events[1]["id"] == "42"


def test_id_with_nul_byte_is_ignored() -> None:
    events = _events(["data: x", "id: bad\x00id", ""])
    assert events[0]["id"] == ""


def test_retry_field_parsed_when_numeric() -> None:
    events = _events(["data: x", "retry: 3000", ""])
    assert events[0]["retry"] == 3000


def test_retry_non_numeric_ignored() -> None:
    events = _events(["data: x", "retry: foo", ""])
    assert events[0]["retry"] is None


def test_space_after_colon_stripped_once() -> None:
    events = _events(["data:  two-spaces", ""])
    # Only the first space after the colon is stripped per WHATWG.
    assert events[0]["data"] == " two-spaces"


def test_field_without_colon_treated_as_empty_value() -> None:
    # "data" alone with no colon is a field name with empty value.
    events = _events(["data", "data: real", ""])
    assert events[0]["data"] == "\nreal"
