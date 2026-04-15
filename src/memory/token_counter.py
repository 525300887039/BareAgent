from __future__ import annotations

import math
import re
from typing import Any

from src.core.fileutil import stringify

_CJK_PATTERN = re.compile(
    "["
    "\u3000-\u303f"
    "\u3040-\u309f"
    "\u30a0-\u30ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uac00-\ud7af"
    "\uf900-\ufaff"
    "\U00020000-\U0002a6df"
    "\U0002f800-\U0002fa1f"
    "]"
)
_ASCII_ALNUM_PATTERN = re.compile(r"[A-Za-z0-9]")
_WHITESPACE_PATTERN = re.compile(r"\s")


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

    return _estimate_text(stringify(value))


def _estimate_text(text: str) -> float:
    cjk = len(_CJK_PATTERN.findall(text))
    ascii_alnum = len(_ASCII_ALNUM_PATTERN.findall(text))
    whitespace = len(_WHITESPACE_PATTERN.findall(text))
    other = len(text) - cjk - ascii_alnum - whitespace
    return cjk * 1.5 + ascii_alnum * 0.25 + whitespace * 0.25 + other * 0.5
