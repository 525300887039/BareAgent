from __future__ import annotations

import json
from typing import Any

import openai

from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str, base_url: str | None = None) -> None:
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.base_url = base_url

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
        }
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            params["tools"] = converted_tools
        params.update(kwargs)

        response = self.client.chat.completions.create(**params)
        return self._parse_response(response)

    def create_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ):
        _ = messages, tools, kwargs
        raise NotImplementedError("OpenAI streaming will be implemented in Task 04.")

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            content = message.get("content", "")
            if role in {"system", "user"}:
                converted.extend(self._convert_non_assistant_message(role, content))
                continue
            if role == "assistant":
                converted.append(self._convert_assistant_message(content))
                continue

            converted.append({"role": role, "content": self._stringify_content(content)})
        return converted

    def _convert_non_assistant_message(
        self,
        role: str,
        content: Any,
    ) -> list[dict[str, Any]]:
        if role != "user":
            return [{"role": role, "content": self._stringify_content(content)}]
        if isinstance(content, str):
            return [{"role": "user", "content": content}]
        if not isinstance(content, list):
            return [{"role": "user", "content": self._stringify_content(content)}]

        converted: list[dict[str, Any]] = []
        trailing_text: list[str] = []
        for block in content:
            if block.get("type") == "tool_result":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": self._stringify_content(block.get("content", "")),
                    }
                )
                continue
            if block.get("type") == "text":
                trailing_text.append(str(block.get("text", "")))
                continue
            trailing_text.append(self._stringify_content(block))

        text = "\n".join(part for part in trailing_text if part)
        if text:
            converted.append({"role": "user", "content": text})
        return converted

    def _convert_assistant_message(self, content: Any) -> dict[str, Any]:
        if isinstance(content, str):
            return {"role": "assistant", "content": content}
        if not isinstance(content, list):
            return {"role": "assistant", "content": self._stringify_content(content)}

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
                continue
            if block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(
                                block.get("input", {}),
                                ensure_ascii=False,
                            ),
                        },
                    }
                )

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(part for part in text_parts if part) or None,
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        return assistant_message

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {"type": "object", "properties": {}},
                    ),
                },
            }
            for tool in tools
        ]

    def _parse_response(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for tool_call in message.tool_calls or []:
            arguments = tool_call.function.arguments or "{}"
            try:
                parsed_input = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_input = {"raw_arguments": arguments}
            if not isinstance(parsed_input, dict):
                parsed_input = {"value": parsed_input}
            tool_calls.append(
                ToolCall(
                    id=tool_call.id,
                    name=tool_call.function.name,
                    input=parsed_input,
                )
            )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=message.content or "",
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

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
