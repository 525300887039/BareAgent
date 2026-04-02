from __future__ import annotations

import json
from typing import Any

import openai

from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall


class OpenAIProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        wire_api: str | None = None,
    ) -> None:
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.base_url = base_url
        self.wire_api = (wire_api or "chat_completions").strip().lower()

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        if self.wire_api == "responses":
            return self._create_via_responses(messages, tools, **kwargs)

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

    def _create_via_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        instructions, input_items = self._convert_messages_for_responses(messages)
        params: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
        }
        if instructions:
            params["instructions"] = instructions
        converted_tools = self._convert_tools_for_responses(tools)
        if converted_tools:
            params["tools"] = converted_tools

        response_kwargs = dict(kwargs)
        if "max_tokens" in response_kwargs and "max_output_tokens" not in response_kwargs:
            response_kwargs["max_output_tokens"] = response_kwargs.pop("max_tokens")
        params.update(response_kwargs)

        raw_response = self.client.responses.create(**params)
        return self._parse_responses_api_response(raw_response)

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

    def _convert_messages_for_responses(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        instruction_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            content = message.get("content", "")
            if role in {"system", "developer"}:
                instruction_text = self._stringify_content(content)
                if instruction_text:
                    instruction_parts.append(instruction_text)
                continue
            if role in {"user", "assistant"}:
                converted.extend(self._convert_response_message(role, content))
                continue

            converted.append(self._make_response_text_message(role, self._stringify_content(content)))
        instructions = "\n\n".join(part for part in instruction_parts if part) or None
        return instructions, converted

    def _convert_response_message(self, role: str, content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [self._make_response_text_message(role, content)]
        if not isinstance(content, list):
            return [self._make_response_text_message(role, self._stringify_content(content))]

        converted: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "tool_result":
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": self._stringify_content(block.get("content", "")),
                    }
                )
                continue
            if block_type == "tool_use":
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    }
                )
                continue
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
                continue
            text_parts.append(self._stringify_content(block))

        text = "\n".join(part for part in text_parts if part)
        if text:
            converted.insert(0, self._make_response_text_message(role, text))
        return converted

    def _make_response_text_message(self, role: str, text: str) -> dict[str, Any]:
        content_type = "output_text" if role == "assistant" else "input_text"
        return {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        }

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

    def _convert_tools_for_responses(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
                "strict": False,
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

    def _parse_responses_api_response(self, response: Any) -> LLMResponse:
        payload = self._coerce_responses_payload(response)
        output_items = payload.get("output", [])

        text_parts: list[str] = []
        content_blocks: list[dict[str, Any]] = []
        tool_calls: list[ToolCall] = []

        for item in output_items:
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") != "output_text":
                        continue
                    text = str(part.get("text", ""))
                    text_parts.append(text)
                    content_blocks.append({"type": "text", "text": text})
                continue
            if item_type != "function_call":
                continue

            arguments = item.get("arguments", "{}")
            try:
                parsed_input = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_input = {"raw_arguments": arguments}
            if not isinstance(parsed_input, dict):
                parsed_input = {"value": parsed_input}

            call_id = str(item.get("call_id", item.get("id", "")))
            name = str(item.get("name", ""))
            tool_calls.append(ToolCall(id=call_id, name=name, input=parsed_input))
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": parsed_input,
                }
            )

        usage = payload.get("usage", {}) or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        stop_reason = "tool_calls" if tool_calls else str(payload.get("status", "completed"))

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            content_blocks=content_blocks,
        )

    def _coerce_responses_payload(self, response: Any) -> dict[str, Any]:
        if isinstance(response, str):
            return self._parse_responses_sse(response)
        if isinstance(response, dict):
            return response
        if hasattr(response, "to_dict"):
            payload = response.to_dict()
            if isinstance(payload, dict):
                return payload
        raise TypeError(f"Unsupported Responses API payload: {type(response).__name__}")

    def _parse_responses_sse(self, payload: str) -> dict[str, Any]:
        last_response: dict[str, Any] | None = None
        for line in payload.splitlines():
            if not line.startswith("data: "):
                continue
            raw_json = line[6:].strip()
            if not raw_json:
                continue
            event = json.loads(raw_json)
            if event.get("type") == "response.completed":
                return dict(event.get("response", {}))
            if isinstance(event.get("response"), dict):
                last_response = dict(event["response"])
        if last_response is not None:
            return last_response
        raise ValueError("Could not parse Responses API payload.")

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
