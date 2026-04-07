from __future__ import annotations

import json
from collections.abc import Generator
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.fileutil import stringify


@dataclass(slots=True)
class ThinkingConfig:
    """Extended thinking settings shared across providers."""

    mode: Literal["enabled", "adaptive", "disabled"] = "adaptive"
    budget_tokens: int = 10000


VALID_THINKING_MODES: frozenset[str] = frozenset({"enabled", "adaptive", "disabled"})


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class StreamEvent:
    type: str
    text: str = ""
    tool_call_id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    content_blocks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    def to_message(self) -> dict[str, Any]:
        """Convert the normalized response back into an assistant message."""
        if self.content_blocks:
            return {
                "role": "assistant",
                "content": [dict(block) for block in self.content_blocks],
            }

        if not self.tool_calls:
            return {"role": "assistant", "content": self.text}

        content: list[dict[str, Any]] = []
        if self.text:
            content.append({"type": "text", "text": self.text})
        for tool_call in self.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "input": tool_call.input,
                }
            )

        return {"role": "assistant", "content": content}


class BaseLLMProvider(ABC):
    @abstractmethod
    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Create a non-streaming response."""

    @abstractmethod
    def create_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Generator[StreamEvent, None, LLMResponse]:
        """Yield streaming events and return the final normalized response."""

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                else:
                    text_parts.append(json.dumps(block, ensure_ascii=False, default=str))
            return "\n".join(part for part in text_parts if part)
        return stringify(content)
