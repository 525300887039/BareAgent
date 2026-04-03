from __future__ import annotations

from typing import Any


def tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "parameters": input_schema,
    }
