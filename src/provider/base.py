from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ThinkingConfig:
    """Extended thinking settings shared across providers."""

    mode: str = "adaptive"
    budget_tokens: int = 10000


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int
    output_tokens: int
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
    ):
        """Yield a streaming response."""
