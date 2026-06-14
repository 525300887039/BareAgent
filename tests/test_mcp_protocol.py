"""Tests for src.mcp.protocol — JSON-RPC 2.0 message codec."""

from __future__ import annotations

import json

import pytest

from bareagent.mcp.errors import MCPProtocolError
from bareagent.mcp.protocol import (
    ErrorObject,
    Notification,
    Request,
    Response,
    decode_message,
    encode_message,
    new_request_id,
)


def test_request_round_trip() -> None:
    req = Request(id=1, method="tools/list", params={"cursor": "abc"})
    line = encode_message(req)
    decoded = decode_message(line)
    assert isinstance(decoded, Request)
    assert decoded.id == 1
    assert decoded.method == "tools/list"
    assert decoded.params == {"cursor": "abc"}


def test_response_result_round_trip() -> None:
    resp = Response(id=2, result={"tools": []})
    decoded = decode_message(encode_message(resp))
    assert isinstance(decoded, Response)
    assert decoded.id == 2
    assert decoded.result == {"tools": []}
    assert decoded.error is None


def test_response_error_round_trip() -> None:
    resp = Response(
        id=3,
        error=ErrorObject(code=-32601, message="Method not found", data={"hint": "x"}),
    )
    decoded = decode_message(encode_message(resp))
    assert isinstance(decoded, Response)
    assert decoded.error is not None
    assert decoded.error.code == -32601
    assert decoded.error.message == "Method not found"
    assert decoded.error.data == {"hint": "x"}


def test_notification_round_trip_no_id() -> None:
    note = Notification(method="notifications/initialized")
    line = encode_message(note)
    payload = json.loads(line)
    assert "id" not in payload
    decoded = decode_message(line)
    assert isinstance(decoded, Notification)
    assert decoded.method == "notifications/initialized"
    assert decoded.params is None


def test_encode_omits_optional_params() -> None:
    req = Request(id=4, method="ping")
    payload = json.loads(encode_message(req))
    assert payload == {"jsonrpc": "2.0", "id": 4, "method": "ping"}


def test_encode_has_no_embedded_newline() -> None:
    req = Request(id=5, method="tools/call", params={"text": "line1\nline2"})
    line = encode_message(req)
    assert "\n" not in line


def test_new_request_id_monotonic_and_threadsafe() -> None:
    ids = [new_request_id() for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_decode_rejects_batch_arrays() -> None:
    with pytest.raises(MCPProtocolError, match="batch"):
        decode_message('[{"jsonrpc":"2.0","id":1,"method":"x"}]')


def test_decode_rejects_missing_jsonrpc() -> None:
    with pytest.raises(MCPProtocolError, match="jsonrpc"):
        decode_message('{"id":1,"method":"x"}')


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        decode_message("not json")


def test_decode_rejects_response_with_both_result_and_error() -> None:
    bad = '{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":-1,"message":"x"}}'
    with pytest.raises(MCPProtocolError, match="exactly one"):
        decode_message(bad)


def test_decode_rejects_envelope_without_method_or_id() -> None:
    with pytest.raises(MCPProtocolError, match="neither method nor id"):
        decode_message('{"jsonrpc":"2.0"}')


def test_decode_accepts_null_id_in_response() -> None:
    # Parse-error responses carry null id per spec.
    decoded = decode_message(
        '{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse"}}'
    )
    assert isinstance(decoded, Response)
    assert decoded.id is None
    assert decoded.error is not None
    assert decoded.error.code == -32700


def test_error_object_omits_data_when_none() -> None:
    err = ErrorObject(code=-32603, message="boom")
    assert err.to_dict() == {"code": -32603, "message": "boom"}
