from __future__ import annotations

from typing import Any

import anthropic

from bareagent.provider.base import (
    BaseLLMProvider,
    CacheConfig,
    LLMResponse,
    StreamEvent,
    ThinkingConfig,
    ToolCall,
)

_PROTECTED_KEYS = frozenset({"model", "messages", "tools", "system", "thinking", "max_tokens"})

# Content-block types that may carry a ``cache_control`` breakpoint. Thinking /
# redacted_thinking blocks must not, so the conversation breakpoint skips a
# trailing thinking block rather than risk an API error.
_CACHEABLE_BLOCK_TYPES = frozenset({"text", "image", "tool_use", "tool_result", "document"})


class AnthropicProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_config: ThinkingConfig | None = None,
        cache_config: CacheConfig | None = None,
    ) -> None:
        # The app layer (src/core/retry.py) owns retries exclusively; disable
        # the SDK's built-in retries to avoid 2xN compound amplification.
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.model = model
        self.thinking_config = thinking_config or ThinkingConfig()
        # None => caching off (legacy byte-identical requests). factory always
        # passes an instance, so the app defaults to caching ON.
        self.cache_config = cache_config

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        params = self._build_request_params(messages, tools, **kwargs)
        response = self.client.messages.create(**params)
        return self._parse_response(response)

    def create_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ):
        params = self._build_request_params(messages, tools, **kwargs)
        with self.client.messages.stream(**params) as stream:
            for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield StreamEvent(type="text", text=event.delta.text)
                    continue

                if event.type != "content_block_stop":
                    continue

                content_block = event.content_block
                if content_block.type != "tool_use":
                    continue

                yield StreamEvent(
                    type="tool_call",
                    tool_call_id=content_block.id,
                    name=content_block.name,
                    input=dict(content_block.input or {}),
                )

            return self._parse_response(stream.get_final_message())

    def _build_request_params(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        system_prompt, anthropic_messages = self._convert_messages(messages)
        max_tokens = int(kwargs.get("max_tokens", 8000))
        if self.thinking_config.mode in {"enabled", "adaptive"}:
            max_tokens = max(max_tokens, self.thinking_config.budget_tokens + 1)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        converted_tools = self._convert_tools(tools)
        system_value: str | list[dict[str, Any]] | None = system_prompt or None

        if self._caching_enabled():
            cache_control = self._cache_control()
            # tools render first, then system, then messages; a breakpoint on
            # the last system block already caches tools+system, but a separate
            # breakpoint on the last tool gives an independent tools-only cache
            # segment (cheap insurance, no double-billing). <=3 breakpoints total
            # (tools, system, last message) — well within Anthropic's max of 4.
            if converted_tools:
                converted_tools[-1] = {**converted_tools[-1], "cache_control": cache_control}
            if system_prompt:
                system_value = [
                    {"type": "text", "text": system_prompt, "cache_control": cache_control}
                ]
            self._apply_conversation_breakpoint(anthropic_messages, cache_control)

        if converted_tools:
            params["tools"] = converted_tools
        if system_value:
            params["system"] = system_value
        if self.thinking_config.mode in {"enabled", "adaptive"}:
            params["thinking"] = {
                "type": self.thinking_config.mode,
                "budget_tokens": self.thinking_config.budget_tokens,
            }
        params.update({k: v for k, v in kwargs.items() if k not in _PROTECTED_KEYS})
        return params

    def _caching_enabled(self) -> bool:
        return self.cache_config is not None and self.cache_config.enabled

    def _cache_control(self) -> dict[str, Any]:
        control: dict[str, Any] = {"type": "ephemeral"}
        if self.cache_config is not None and self.cache_config.ttl == "1h":
            control["ttl"] = "1h"
        return control

    def _apply_conversation_breakpoint(
        self,
        messages: list[dict[str, Any]],
        cache_control: dict[str, Any],
    ) -> None:
        """Attach a ``cache_control`` breakpoint to the last message's last block.

        This is the moving incremental-caching breakpoint: each request only
        appends a couple of blocks since the previous one, so the 20-block
        lookback reliably finds the prior cached prefix. The message dicts here
        are freshly built by ``_convert_messages`` (not shared with the caller),
        so in-place mutation is safe.
        """
        if not messages:
            return
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, str):
            if content:
                last["content"] = [
                    {"type": "text", "text": content, "cache_control": cache_control}
                ]
            return
        if isinstance(content, list) and content:
            last_block = content[-1]
            if last_block.get("type") in _CACHEABLE_BLOCK_TYPES:
                content[-1] = {**last_block, "cache_control": cache_control}

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
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                )
                continue
            if block_type == "tool_result":
                result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id", ""),
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
                if not isinstance(item, dict):
                    blocks.append({"type": "text", "text": self._stringify_content(item)})
                    continue
                item_type = item.get("type")
                if item_type == "text":
                    blocks.append({"type": "text", "text": item.get("text", "")})
                    continue
                if item_type == "image":
                    # BareAgent's internal image shape is already Anthropic-native.
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                        and source.get("data")
                    ):
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": source.get("media_type", "image/png"),
                                    "data": source.get("data", ""),
                                },
                            }
                        )
                        continue
                    blocks.append({"type": "text", "text": self._stringify_content(item)})
                    continue
                blocks.append({"type": "text", "text": self._stringify_content(item)})
            return blocks
        return self._stringify_content(content)

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
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            thinking="\n\n".join(part for part in thinking_parts if part),
            content_blocks=content_blocks,
        )
