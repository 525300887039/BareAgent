"""Minimal Server-Sent Events parser per WHATWG.

Used by both HTTP transports. Pure-functional: takes an iterable of already-
split lines (no trailing newlines) and yields event dicts. Last-Event-ID
reconnect is deferred to a later PR.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypedDict

_BOM = "﻿"


class SSEEvent(TypedDict):
    event: str
    data: str
    id: str
    retry: int | None


def parse_sse_stream(lines: Iterable[str]) -> Iterator[SSEEvent]:
    """Yield SSE events from already-split lines.

    Caller must split on `\\r?\\n` and not include trailing line terminators
    (httpx `iter_lines()` already does this). Empty line dispatches an event.
    """
    event_type = ""
    data: list[str] = []
    last_id = ""
    retry: int | None = None
    first = True

    for raw in lines:
        if first:
            first = False
            if raw.startswith(_BOM):
                raw = raw[1:]
        if raw == "":
            if data:
                yield SSEEvent(
                    event=event_type or "message",
                    data="\n".join(data),
                    id=last_id,
                    retry=retry,
                )
            event_type = ""
            data = []
            retry = None
            continue
        if raw.startswith(":"):
            continue  # comment / heartbeat
        field, sep, value = raw.partition(":")
        if not sep:
            # No colon means the whole line is the field name with empty value.
            field, value = raw, ""
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            if "\x00" not in value:
                last_id = value
        elif field == "retry" and value.isdigit():
            retry = int(value)
        # unknown fields are silently ignored per spec
