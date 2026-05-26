# SSE Minimal Parser for MCP HTTP/SSE Transport — Research Notes

## SSE Wire Format (WHATWG)

| Field   | Semantics                                                                                                                          |
|---------|------------------------------------------------------------------------------------------------------------------------------------|
| `event` | Set event type buffer to field value. Defaults to `"message"` if omitted on dispatch.                                              |
| `data`  | Append value + a single `\n` to the data buffer. Multiple `data:` lines concatenate, trailing `\n` is stripped on dispatch.        |
| `id`    | Set the last-event-ID buffer (ignored if value contains NUL).                                                                      |
| `retry` | If value is all ASCII digits, set reconnection time in ms.                                                                         |

Parsing rules:
- Stream MUST be UTF-8; strip one leading BOM if present.
- Line endings: `\r\n`, `\r`, or `\n`. Split lines accordingly.
- A line starting with `:` is a **comment** — ignored. Servers send `:keepalive\n\n` as heartbeats to defeat proxy idle timeouts.
- A line without `:` is treated as a field name with empty value.
- A line `key: value` (one space after colon is stripped) is a field.
- **Empty line dispatches the event** using the accumulated buffers; buffers (data + event-type) reset, but `last-event-id` persists.
- If after dispatch the data buffer is empty, no event is dispatched (but event-type resets).

Multi-line example (one event with two-line data):
```
event: message
data: {"jsonrpc":"2.0",
data: "id":1,"result":{}}
id: 42

```
Dispatched data field = `{"jsonrpc":"2.0",\n"id":1,"result":{}}`.

Comment / heartbeat example:
```
: ping

```

## HTTP Layer Requirements

- Client request header: `Accept: text/event-stream` (MCP requires also `application/json` for the POST case).
- Server response: `Content-Type: text/event-stream`, typically `Cache-Control: no-cache`, `Connection: keep-alive`, often chunked transfer encoding.
- Long-lived connection: no read timeout on the body; only connect/write timeouts.
- Reconnect: client SHOULD send `Last-Event-ID: <id>` on the GET to resume.

## httpx Streaming API Skeleton

```python
import httpx

timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
headers = {"Accept": "text/event-stream"}
async with httpx.AsyncClient(timeout=timeout) as client:
    async with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():  # splits on \r?\n, no trailing newline
            ...  # feed into SSE parser
```

Cancellation: exit the `async with` block (or call `await resp.aclose()`); httpx closes the underlying connection. Use `asyncio.CancelledError` propagation cleanly.

## MCP on SSE

Two flavors exist in the wild — BareAgent should target the **legacy 2024-11-05 HTTP+SSE** first (simpler, what most current public servers expose), with a path to **Streamable HTTP (2025-03-26)** later.

### Legacy HTTP+SSE (2024-11-05)
- Two endpoints: a `GET /sse` stream + a `POST` write endpoint.
- First SSE event MUST be `event: endpoint` with `data:` = relative URI (a **plain string**, not JSON) the client POSTs to for upstream messages. Often contains a session token, e.g. `/messages?session_id=abc`.
- All subsequent server messages: `event: message`, `data:` = one JSON-RPC envelope.
- Client→server: HTTP POST JSON-RPC body to the endpoint URI. Server replies with 202 Accepted; the actual JSON-RPC response arrives on the SSE stream.

### Streamable HTTP (2025-03-26, supersedes the above)
- Single MCP endpoint accepts both POST (upstream) and GET (open server-push SSE).
- Each POST that contains a JSON-RPC request: server replies either `Content-Type: application/json` (single object, no SSE) or `Content-Type: text/event-stream` (SSE with one `event: message` per response, then closes).
- Each SSE `data:` is exactly one JSON-RPC message (request, response, or notification). The `event` field is always `message` (no semantic typing — the JSON-RPC envelope itself carries the type).
- Session via `Mcp-Session-Id` header.
- Resumability: server may set `id:` per event; client reconnects with `Last-Event-ID`.

Conclusion: the `event` field is only semantically meaningful for the legacy `endpoint` event. Everything else is `message` carrying a JSON-RPC payload.

## Minimal Parser (Python, <50 lines)

```python
from typing import Iterable, Iterator

def parse_sse_lines(lines: Iterable[str]) -> Iterator[dict]:
    """Parse SSE per WHATWG. Yields dicts {event, data, id, retry}.
    `lines` MUST be already split on \\r?\\n with no trailing newline
    (httpx aiter_lines / iter_lines satisfies this)."""
    event = ""
    data: list[str] = []
    last_id = ""
    retry: int | None = None
    for raw in lines:
        # Strip a single leading BOM on the very first line only.
        if raw.startswith("﻿"):
            raw = raw[1:]
        if raw == "":  # dispatch
            if data:
                yield {
                    "event": event or "message",
                    "data": "\n".join(data),
                    "id": last_id,
                    "retry": retry,
                }
            event, data, retry = "", [], None
            continue
        if raw.startswith(":"):  # comment / heartbeat
            continue
        field, _, value = raw.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            if "\x00" not in value:
                last_id = value
        elif field == "retry" and value.isdigit():
            retry = int(value)
        # unknown fields ignored
```

Async wrapper (the real entry point):

```python
async def sse_events(client, url, headers):
    async with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        # aiter_lines yields without trailing newlines; "" marks dispatch.
        async def gen():
            async for line in resp.aiter_lines():
                yield line
        # Bridge sync generator: collect lines into a queue or inline-port parse_sse_lines as async.
```

In practice port `parse_sse_lines` to an `async def` taking `resp.aiter_lines()` — same body, `async for`.

## Implications for BareAgent

Suggested layout `src/mcp/transport/http_sse.py`:
- `class SseTransport`: takes base URL, opens GET stream, parses with the function above.
- Tracks `endpoint_url` (set on first `endpoint` event for legacy mode) and `last_event_id`.
- Exposes `async def send(msg)` → POST to endpoint, `async def recv()` → async iterator of parsed JSON-RPC dicts (from `message` events).
- Auto-detect legacy vs streamable: if first event is `endpoint`, switch to legacy; else treat as streamable.
- Reconnect loop on stream end / network error: backoff using `retry` value (default 3000 ms), include `Last-Event-ID` header.

Test surface (mock `httpx.MockTransport` or feed `parse_sse_lines` directly):
1. Single `event: message` + single-line `data` → one dispatch with parsed JSON.
2. Multi-line `data` → joined with `\n`.
3. Comment lines (`: ping`) → no dispatch, stream stays alive.
4. `event: endpoint` first → legacy mode, captured URI string.
5. `id:` updates last-event-id; reconnect sends `Last-Event-ID`.
6. `\r\n`, `\r`, `\n` all work (httpx normalizes — verify in test).
7. Empty `data` buffer + empty line → no spurious dispatch.
8. BOM at start → stripped.
9. Stream closes mid-event (no dispatch line) → partial buffer discarded, no half-event yielded.
10. POST returns 202 with no body → no error.

## Sources
- [WHATWG HTML Living Standard — Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- [MCP spec 2024-11-05 — Transports (HTTP+SSE)](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)
- [MCP spec 2025-03-26 — Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
- [httpx async streaming docs](https://www.python-httpx.org/async/)
- [MCP legacy concepts — Transports](https://modelcontextprotocol.io/legacy/concepts/transports)
