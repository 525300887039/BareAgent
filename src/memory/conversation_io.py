"""Conversation import/export serialization (REPL-independent, unit-testable).

Pure helpers backing the ``/export`` and ``/import`` REPL commands:

- :func:`render_markdown` — human-readable Markdown (mirrors the traversal
  structure of ``main._replay_stdio_transcript``).
- :func:`to_export_json` — self-contained, faithful JSON wrapper.
- :func:`parse_import` — auto-detecting loader with shape validation.

No dependency on ``src.main`` / UI / loop, so this module can be exercised in
isolation.
"""

from __future__ import annotations

import json
from typing import Any

EXPORT_VERSION = 1
_DEFAULT_MAX_TOOL_CHARS = 2000
_TOOL_INPUT_PREVIEW_CHARS = 200
_TRUNCATION_MARKER = "… (truncated)"


def _truncate(text: str, limit: int) -> str:
    if limit < 0 or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _stringify_content(content: Any) -> str:
    """Coerce arbitrary tool_result content into display text."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


def _tool_use_summary(block: dict[str, Any]) -> str:
    name = str(block.get("name", "unknown"))
    raw_input = block.get("input", {})
    preview = json.dumps(raw_input, ensure_ascii=False, default=str)
    preview = _truncate(preview, _TOOL_INPUT_PREVIEW_CHARS)
    return f"- **Tool call** `{name}`: `{preview}`"


def render_markdown(
    messages: list[dict[str, Any]],
    *,
    include_thinking: bool = False,
    max_tool_chars: int = _DEFAULT_MAX_TOOL_CHARS,
    title: str | None = None,
) -> str:
    """Render *messages* as human-readable Markdown.

    Skips ``system`` messages, renders user/assistant text, turns ``tool_use``
    blocks into single-line summaries and ``tool_result`` blocks into truncated
    code fences. ``thinking`` blocks are omitted unless *include_thinking* is
    set. Mirrors the traversal of ``main._replay_stdio_transcript`` (including
    the ``tool_use_id`` → tool name association). *title*, when provided, is
    emitted as a top-level heading.
    """
    tool_name_by_id: dict[str, str] = {}
    lines: list[str] = []

    if title:
        lines.append(f"# {title}")
        lines.append("")

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            continue

        if role == "user":
            if isinstance(content, str):
                lines.append("## User")
                lines.append("")
                lines.append(content)
                lines.append("")
                continue
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_value = str(block.get("text", ""))
                        if text_value:
                            lines.append("## User")
                            lines.append("")
                            lines.append(text_value)
                            lines.append("")
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_name = tool_name_by_id.get(
                        str(block.get("tool_use_id", "")),
                        "unknown",
                    )
                    result_text = _truncate(
                        _stringify_content(block.get("content", "")),
                        max_tool_chars,
                    )
                    error_marker = " (error)" if block.get("is_error") else ""
                    lines.append(f"### Tool result: {tool_name}{error_marker}")
                    lines.append("")
                    lines.append("```")
                    lines.append(result_text)
                    lines.append("```")
                    lines.append("")
            continue

        if role != "assistant":
            continue

        if isinstance(content, str):
            lines.append("## Assistant")
            lines.append("")
            lines.append(content)
            lines.append("")
            continue
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_value = str(block.get("text", ""))
                if text_value:
                    lines.append("## Assistant")
                    lines.append("")
                    lines.append(text_value)
                    lines.append("")
                continue
            if block_type == "thinking":
                if not include_thinking:
                    continue
                thinking_value = str(block.get("thinking", ""))
                if thinking_value:
                    lines.append("### Thinking")
                    lines.append("")
                    lines.append("```")
                    lines.append(thinking_value)
                    lines.append("```")
                    lines.append("")
                continue
            if block_type != "tool_use":
                continue

            tool_id = str(block.get("id", ""))
            if tool_id:
                tool_name_by_id[tool_id] = str(block.get("name", "unknown"))
            lines.append(_tool_use_summary(block))
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def to_export_json(
    messages: list[dict[str, Any]],
    *,
    session_id: str,
    exported_at: str,
) -> str:
    """Serialize *messages* into a self-contained, faithful JSON wrapper.

    The wrapper preserves every message verbatim (including ``system`` /
    ``thinking`` / tool blocks) so a round-trip through :func:`parse_import`
    recovers an equivalent conversation.
    """
    payload = {
        "version": EXPORT_VERSION,
        "session_id": session_id,
        "exported_at": exported_at,
        "messages": messages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("conversation must be a list of messages")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"message at index {index} is not an object")
        if "role" not in item:
            raise ValueError(f"message at index {index} is missing a 'role' key")
    return value


def parse_import(text: str) -> list[dict[str, Any]]:
    """Parse imported conversation *text* into validated messages.

    Auto-detects the format: first try parsing the whole document as JSON — a
    dict with a ``messages`` key yields those messages, a bare list is used
    directly. If whole-document JSON parsing fails, fall back to JSONL
    (one JSON object per non-blank line).

    Validation: the result must be a list where every element is a dict
    containing a ``role`` key; otherwise :class:`ValueError` is raised with a
    human-readable reason. No other rewriting is performed (faithful load).
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        messages: list[Any] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                messages.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
        return _validate_messages(messages)

    if isinstance(document, dict):
        if "messages" not in document:
            raise ValueError("JSON object is missing a 'messages' key")
        return _validate_messages(document["messages"])
    return _validate_messages(document)
