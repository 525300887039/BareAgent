from __future__ import annotations

import json
import math
from typing import Any


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token usage with a lightweight character heuristic."""
    total = 0.0
    for message in messages:
        total += _estimate_value(message.get("content"))
    return int(math.ceil(total))


def _estimate_value(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        return _estimate_text(value)
    if isinstance(value, list):
        return sum(_estimate_value(item) for item in value)
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type == "tool_use":
            return _estimate_text(str(value.get("name", ""))) + _estimate_value(
                value.get("input")
            )

        total = 0.0
        if "text" in value:
            total += _estimate_value(value.get("text"))
        if "content" in value:
            total += _estimate_value(value.get("content"))
        if "input" in value:
            total += _estimate_value(value.get("input"))
        if "name" in value and block_type != "tool_result":
            total += _estimate_text(str(value.get("name", "")))
        return total

    return _estimate_text(_stringify(value))


def _estimate_text(text: str) -> float:
    total = 0.0
    for char in text:
        if _is_cjk(char):
            total += 1.5
        elif char.isascii() and char.isalnum():
            total += 0.25
        elif char.isspace():
            continue
        else:
            total += 0.5
    return total


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
