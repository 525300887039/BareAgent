"""Shared file-system and small utilities."""

from __future__ import annotations

import json
import os
import secrets
import string
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ID_ALPHABET = string.ascii_letters + string.digits


def stringify(value: Any) -> str:
    """Convert any value to a string suitable for tool output or serialization."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def generate_random_id(length: int = 8) -> str:
    """Return a cryptographically random alphanumeric string."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def is_tool_result_message(msg: dict[str, Any]) -> bool:
    """Check whether a message contains tool_result blocks."""
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def atomic_write_json(file_path: Path, payload: Any) -> None:
    """Atomically write *payload* as JSON to *file_path* via tempfile + rename."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(file_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def utc_timestamp_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def optional_string(value: Any) -> str | None:
    """Normalize *value* to a stripped string, or ``None`` if blank/None."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def collect_tool_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Build a mapping from tool_use id → tool name across all messages."""
    tool_name_by_id: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = str(block.get("id", ""))
            if tool_id:
                tool_name_by_id[tool_id] = str(block.get("name", "unknown"))
    return tool_name_by_id
