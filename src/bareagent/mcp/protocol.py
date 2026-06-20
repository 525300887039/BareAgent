"""JSON-RPC 2.0 message types and codec for MCP.

MCP `2025-06-18` removed batch support, so this module deliberately does not
implement JSON-RPC batch arrays — top-level arrays are rejected by callers.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from .errors import MCPProtocolError

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Server error range (application-defined): -32000 .. -32099.
SERVER_ERROR_MIN = -32099
SERVER_ERROR_MAX = -32000

JSONRPC_VERSION = "2.0"


@dataclass(slots=True)
class ErrorObject:
    """JSON-RPC error object embedded in a Response."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass(slots=True)
class Request:
    """Outbound or inbound JSON-RPC request (has an id)."""

    id: int
    method: str
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.id,
            "method": self.method,
        }
        if self.params is not None:
            payload["params"] = self.params
        return payload


@dataclass(slots=True)
class Response:
    """JSON-RPC response: either result or error, never both."""

    id: int | None
    result: Any = None
    error: ErrorObject | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": self.id}
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        else:
            payload["result"] = self.result
        return payload


@dataclass(slots=True)
class Notification:
    """JSON-RPC notification: a request without an id (never gets a reply)."""

    method: str
    params: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": self.method}
        if self.params is not None:
            payload["params"] = self.params
        return payload


_id_counter = itertools.count(1)
_id_lock = threading.Lock()


def new_request_id() -> int:
    """Return a monotonically increasing request id (threadsafe)."""
    with _id_lock:
        return next(_id_counter)


def encode_message(msg: Request | Response | Notification) -> str:
    """Serialize a message to a single-line JSON string (no trailing newline).

    Callers are responsible for appending the framing delimiter (e.g. `\\n` for
    stdio NDJSON). The compact separators guarantee the encoded form contains
    no embedded newline, which is required by MCP stdio framing.
    """
    line = json.dumps(msg.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if "\n" in line:  # pragma: no cover — defensive; json never emits raw \n
        raise MCPProtocolError("encoded JSON-RPC message contains embedded newline")
    return line


def decode_message(line: str) -> Request | Response | Notification:
    """Parse a single JSON-RPC envelope.

    Raises `MCPProtocolError` for malformed input, batch arrays (unsupported in
    MCP 2025-06-18), or envelopes missing required JSON-RPC fields.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise MCPProtocolError(f"invalid JSON: {exc.msg}") from exc

    if isinstance(payload, list):
        raise MCPProtocolError("JSON-RPC batch arrays are not supported (MCP 2025-06-18)")
    if not isinstance(payload, dict):
        raise MCPProtocolError(f"JSON-RPC envelope must be an object, got {type(payload).__name__}")

    if payload.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPProtocolError(f"missing or wrong jsonrpc version: {payload.get('jsonrpc')!r}")

    if "method" in payload:
        method = payload["method"]
        if not isinstance(method, str):
            raise MCPProtocolError(f"method must be a string, got {type(method).__name__}")
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            raise MCPProtocolError("params must be an object")
        if "id" in payload:
            msg_id = payload["id"]
            if not isinstance(msg_id, int):
                raise MCPProtocolError(f"request id must be an integer, got {msg_id!r}")
            return Request(id=msg_id, method=method, params=params)
        return Notification(method=method, params=params)

    # Response: has id + (result xor error)
    if "id" not in payload:
        raise MCPProtocolError("envelope has neither method nor id")
    msg_id = payload["id"]
    if msg_id is not None and not isinstance(msg_id, int):
        raise MCPProtocolError(f"response id must be int or null, got {msg_id!r}")

    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        raise MCPProtocolError("response must contain exactly one of 'result' or 'error'")

    if has_error:
        err = payload["error"]
        if not isinstance(err, dict):
            raise MCPProtocolError("error must be an object")
        try:
            code = int(err["code"])
            message = str(err["message"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MCPProtocolError(f"malformed error object: {exc}") from exc
        return Response(
            id=msg_id,
            error=ErrorObject(code=code, message=message, data=err.get("data")),
        )

    return Response(id=msg_id, result=payload["result"])
