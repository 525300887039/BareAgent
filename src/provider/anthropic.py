from __future__ import annotations

import json
from typing import Any

import anthropic

from src.provider.base import BaseLLMProvider, LLMResponse, ThinkingConfig, ToolCall


class AnthropicProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_config: ThinkingConfig | None = None,
    ) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.thinking_config = thinking_config or ThinkingConfig()

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        system_prompt, anthropic_messages = self._convert_messages(messages)
        max_tokens = int(kwargs.pop("max_tokens", 8000))
        if self.thinking_config.mode in {"enabled", "adaptive"}:
            max_tokens = max(max_tokens, self.thinking_config.budget_tokens + 1)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            params["tools"] = converted_tools
        if system_prompt:
            params["system"] = system_prompt
        if self.thinking_config.mode in {"enabled", "adaptive"}:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_config.budget_tokens,
            }
        params.update(kwargs)

        response = self.client.messages.create(**params)
        return self._parse_response(response)

    def create_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ):
        _ = messages, tools, kwargs
        raise NotImplementedError("Anthropic streaming will be implemented in Task 04.")

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            role = message["role"]
            content = message.get("content", "")
            if role == "system":
                text = self._stringify_content(content)
                if text:
                    system_parts.append(text)
                continue

            converted.append(
                {
                    "role": role,
                    "content": self._convert_message_content(content),
                }
            )

        system_prompt = "\n\n".join(part for part in system_parts if part) or None
        return system_prompt, converted

    def _convert_message_content(self, content: Any) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)

        converted_blocks: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                converted_blocks.append({"type": "text", "text": block.get("text", "")})
                continue
            if block_type == "tool_use":
                converted_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                )
                continue
            if block_type == "tool_result":
                result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block["tool_use_id"],
                    "content": self._convert_tool_result_content(block.get("content", "")),
                }
                if block.get("is_error"):
                    result_block["is_error"] = True
                converted_blocks.append(result_block)
                continue
            if block_type == "thinking" and block.get("signature"):
                converted_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": block["signature"],
                    }
                )
                continue
            if block_type == "redacted_thinking":
                converted_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": block.get("data", ""),
                    }
                )
                continue

        return converted_blocks

    def _convert_tool_result_content(self, content: Any) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    blocks.append({"type": "text", "text": item.get("text", "")})
                else:
                    blocks.append({"type": "text", "text": self._stringify_content(item)})
            return blocks
        return self._stringify_content(content)

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                else:
                    text_parts.append(json.dumps(block, ensure_ascii=False, default=str))
            return "\n".join(part for part in text_parts if part)
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, default=str)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            for tool in tools
        ]

    def _parse_response(self, response: Any) -> LLMResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict[str, Any]] = []

        for block in getattr(response, "content", []):
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text = getattr(block, "text", "")
                text_parts.append(text)
                content_blocks.append({"type": "text", "text": text})
            elif block_type == "thinking":
                thinking = getattr(block, "thinking", "")
                signature = getattr(block, "signature", "")
                thinking_parts.append(thinking)
                thinking_block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": thinking,
                }
                if signature:
                    thinking_block["signature"] = signature
                content_blocks.append(thinking_block)
            elif block_type == "redacted_thinking":
                content_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": getattr(block, "data", ""),
                    }
                )
            elif block_type == "tool_use":
                tool_input = dict(getattr(block, "input", {}) or {})
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=tool_input,
                    )
                )
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": tool_input,
                    }
                )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=getattr(response, "stop_reason", "") or "",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            thinking="\n\n".join(part for part in thinking_parts if part),
            content_blocks=content_blocks,
        )
