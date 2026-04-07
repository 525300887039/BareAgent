from __future__ import annotations

import copy
import logging
from typing import Any

from src.core.fileutil import collect_tool_names, is_tool_result_message, stringify
from src.memory.token_counter import estimate_tokens
from src.memory.transcript import TranscriptManager
from src.provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)

_TRUNCATED_PREFIX = "[truncated:"
_SUMMARY_SYSTEM_PROMPT = (
    "你是 BareAgent 的上下文压缩助手。请用中文总结对话中的目标、已完成工作、"
    "关键约束、重要文件路径、工具执行结果要点，以及接下来继续工作所需的上下文。"
    "输出简洁但不能遗漏事实。"
)


def _micro_compact(
    messages: list[dict[str, Any]],
    keep_recent: int = 3,
    tool_name_by_id: dict[str, str] | None = None,
) -> dict[str, str]:
    if tool_name_by_id is None:
        tool_name_by_id = collect_tool_names(messages)
    tool_result_indices = [
        index for index, message in enumerate(messages) if is_tool_result_message(message)
    ]
    if keep_recent > 0:
        compact_indices = set(tool_result_indices[:-keep_recent])
    else:
        compact_indices = set(tool_result_indices)

    for index in compact_indices:
        message = messages[index]
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            original_text = stringify(block.get("content", ""))
            if original_text.startswith(_TRUNCATED_PREFIX):
                continue
            tool_use_id = str(block.get("tool_use_id", ""))
            tool_name = tool_name_by_id.get(tool_use_id, "unknown")
            block["content"] = (
                f"[truncated: {tool_name} result, {len(original_text)} chars]"
            )
    return tool_name_by_id


def _serialize(
    messages: list[dict[str, Any]],
    tool_name_by_id: dict[str, str] | None = None,
) -> str:
    lines: list[str] = []
    if tool_name_by_id is None:
        tool_name_by_id = collect_tool_names(messages)
    for message in messages:
        role = str(message.get("role", "unknown"))
        lines.append(f"[{role}]")
        lines.append(_serialize_content(message.get("content"), tool_name_by_id))
        lines.append("")
    return "\n".join(lines).strip()


class Compactor:
    def __init__(
        self,
        provider: BaseLLMProvider,
        transcript_mgr: TranscriptManager | None,
        threshold: int = 50000,
        session_id: str = "default",
    ) -> None:
        self._provider = provider
        self._transcript_mgr = transcript_mgr
        self._threshold = threshold
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id

    def set_session_id(self, new_session_id: str) -> None:
        self._session_id = new_session_id

    def __call__(self, messages: list[dict[str, Any]], force: bool = False) -> None:
        if not force and estimate_tokens(messages) <= self._threshold:
            return

        _backup = [_clone_message(m) for m in messages]

        tool_name_by_id = _micro_compact(messages, keep_recent=3)

        history_messages, pending_user_message = _split_pending_user_turn(messages)
        summary_source_messages = [
            message for message in history_messages if message.get("role") != "system"
        ]
        if not summary_source_messages:
            messages[:] = _backup
            return

        if self._transcript_mgr is not None:
            self._transcript_mgr.save(messages, self._session_id)
        try:
            summary = self._provider.create(
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "请简洁总结以下对话的关键信息和已完成的工作，供后续继续工作使用：\n\n"
                            + _serialize(summary_source_messages, tool_name_by_id)
                        ),
                    },
                ],
                tools=[],
                max_tokens=2000,
            )
        except Exception:
            logger.warning("Context compression failed", exc_info=True)
            messages[:] = _backup
            return

        system_messages = [
            _clone_message(message) for message in messages if message.get("role") == "system"
        ]
        messages.clear()
        messages.extend(system_messages)
        messages.extend(
            [
                {"role": "user", "content": f"[Context Compressed]\n{summary.text}"},
                {"role": "assistant", "content": "收到，我已理解之前的上下文，继续工作。"},
            ]
        )
        if pending_user_message is not None:
            messages.append(pending_user_message)


make_compact_fn = Compactor


def _serialize_content(content: Any, tool_name_by_id: dict[str, str]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            serialized = _serialize_block(block, tool_name_by_id)
            if serialized:
                parts.append(serialized)
        return "\n".join(parts)
    return stringify(content)


def _serialize_block(block: Any, tool_name_by_id: dict[str, str]) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return stringify(block)

    block_type = block.get("type")
    if block_type == "text":
        return str(block.get("text", ""))
    if block_type == "tool_use":
        return (
            f"[tool_use:{block.get('name', 'unknown')}] "
            f"{stringify(block.get('input', {}))}"
        )
    if block_type == "tool_result":
        tool_use_id = str(block.get("tool_use_id", ""))
        tool_name = tool_name_by_id.get(tool_use_id, "unknown")
        return f"[tool_result:{tool_name}] {stringify(block.get('content', ''))}"

    if "content" in block:
        return _serialize_content(block.get("content"), tool_name_by_id)
    if "text" in block:
        return str(block.get("text", ""))
    return stringify(block)


def _clone_message(message: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(message)


def _split_pending_user_turn(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if messages and messages[-1].get("role") == "user":
        return messages[:-1], _clone_message(messages[-1])
    return messages, None
