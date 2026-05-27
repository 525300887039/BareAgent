"""PR5: multimodal MCP result passthrough + dual-provider adaptation tests.

Covers:

- :func:`src.mcp.registry._to_content_blocks` — normalize MCP content arrays
  into BareAgent-internal blocks (Anthropic-native image shape + text
  placeholders for everything else).
- :func:`src.core.loop._tool_result` — dual signature (``str`` legacy path
  vs. ``list[dict]`` multimodal path).
- :meth:`AnthropicProvider._convert_tool_result_content` — image
  passthrough.
- :meth:`OpenAIProvider._convert_non_assistant_message` — image "lift" into
  a follow-up user message.
- End-to-end integration: MCP handler -> loop -> provider serialization.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from src.core.loop import _tool_result
from src.mcp.manager import ServerStatus
from src.mcp.registry import _to_content_blocks, build_mcp_handlers
from src.provider.anthropic import AnthropicProvider
from src.provider.openai import OpenAIProvider


# --- _to_content_blocks: unit coverage of every MCP content kind ------------


def test_to_content_blocks_text_passthrough() -> None:
    out = _to_content_blocks(
        [
            {"type": "text", "text": "alpha"},
            {"type": "text", "text": "beta"},
        ]
    )
    assert out == [
        {"type": "text", "text": "alpha"},
        {"type": "text", "text": "beta"},
    ]


def test_to_content_blocks_image_png_jpeg_gif_webp_all_supported() -> None:
    """All four Anthropic-supported image mime types pass through with the
    correct Anthropic-native ``source`` shape."""
    for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        out = _to_content_blocks([{"type": "image", "data": "AAAA", "mimeType": mime}])
        assert out == [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": "AAAA",
                },
            }
        ]


def test_to_content_blocks_image_unsupported_mime_degrades() -> None:
    out = _to_content_blocks(
        [{"type": "image", "data": "AAAA", "mimeType": "image/svg+xml"}]
    )
    assert out == [
        {
            "type": "text",
            "text": "[Image omitted: unsupported mime type 'image/svg+xml']",
        }
    ]


def test_to_content_blocks_image_missing_data_degrades() -> None:
    out = _to_content_blocks([{"type": "image", "mimeType": "image/png"}])
    assert out == [{"type": "text", "text": "[Image omitted: empty data]"}]


def test_to_content_blocks_image_empty_string_data_degrades() -> None:
    out = _to_content_blocks([{"type": "image", "data": "", "mimeType": "image/png"}])
    assert out == [{"type": "text", "text": "[Image omitted: empty data]"}]


def test_to_content_blocks_image_missing_mimetype_degrades() -> None:
    out = _to_content_blocks([{"type": "image", "data": "AAAA"}])
    assert out == [{"type": "text", "text": "[Image omitted: missing mimeType]"}]


def test_to_content_blocks_audio_degrades_to_placeholder() -> None:
    out = _to_content_blocks(
        [{"type": "audio", "data": "ZmFrZQ==", "mimeType": "audio/wav"}]
    )
    assert out == [
        {
            "type": "text",
            "text": "[Audio omitted: not supported by current providers]",
        }
    ]


def test_to_content_blocks_embedded_resource_emits_uri_placeholder() -> None:
    out = _to_content_blocks(
        [
            {
                "type": "embedded_resource",
                "resource": {
                    "uri": "file:///report.pdf",
                    "mimeType": "application/pdf",
                },
            }
        ]
    )
    assert out == [
        {"type": "text", "text": "[Resource: file:///report.pdf (application/pdf)]"}
    ]


def test_to_content_blocks_embedded_resource_missing_fields_uses_unknown() -> None:
    out = _to_content_blocks(
        [
            {"type": "embedded_resource", "resource": {}},
            {"type": "embedded_resource"},  # no `resource` field at all
        ]
    )
    assert out == [
        {"type": "text", "text": "[Resource: unknown (unknown)]"},
        {"type": "text", "text": "[Resource: unknown (unknown)]"},
    ]


def test_to_content_blocks_resource_link_emits_uri_placeholder() -> None:
    out = _to_content_blocks(
        [{"type": "resource_link", "uri": "https://example.com/x.png"}]
    )
    assert out == [
        {"type": "text", "text": "[Resource link: https://example.com/x.png]"}
    ]


def test_to_content_blocks_resource_link_missing_uri_uses_unknown() -> None:
    out = _to_content_blocks([{"type": "resource_link"}])
    assert out == [{"type": "text", "text": "[Resource link: unknown]"}]


def test_to_content_blocks_unknown_type_emits_named_placeholder() -> None:
    out = _to_content_blocks([{"type": "weird_type", "payload": 42}])
    assert out == [{"type": "text", "text": "[Unknown content block: weird_type]"}]


def test_to_content_blocks_missing_type_uses_unknown() -> None:
    out = _to_content_blocks([{}])
    assert out == [{"type": "text", "text": "[Unknown content block: unknown]"}]


def test_to_content_blocks_non_dict_block_does_not_crash() -> None:
    out = _to_content_blocks([None, "stray-string", {"type": "text", "text": "ok"}])  # type: ignore[list-item]
    # Order must be preserved; non-dict blocks become placeholders, real blocks pass through.
    assert out[-1] == {"type": "text", "text": "ok"}
    assert all("Unknown content block" in b["text"] for b in out[:2])


def test_to_content_blocks_mixed_order_preserved() -> None:
    out = _to_content_blocks(
        [
            {"type": "text", "text": "before"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            {"type": "audio"},
            {"type": "text", "text": "after"},
        ]
    )
    assert [block.get("type") for block in out] == ["text", "image", "text", "text"]
    assert out[0]["text"] == "before"
    assert out[1]["source"]["media_type"] == "image/png"
    assert out[2]["text"] == "[Audio omitted: not supported by current providers]"
    assert out[3]["text"] == "after"


# --- _tool_result: dual signature -------------------------------------------


def test_tool_result_string_legacy_path_unchanged() -> None:
    out = _tool_result("tool_1", "plain text")
    assert out == {
        "type": "tool_result",
        "tool_use_id": "tool_1",
        "content": "plain text",
    }


def test_tool_result_string_is_error_flag() -> None:
    out = _tool_result("tool_1", "boom", is_error=True)
    assert out == {
        "type": "tool_result",
        "tool_use_id": "tool_1",
        "content": "boom",
        "is_error": True,
    }


def test_tool_result_list_passthrough_does_not_stringify() -> None:
    blocks = [
        {"type": "text", "text": "label"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAAA",
            },
        },
    ]
    out = _tool_result("tool_2", blocks)
    assert out == {
        "type": "tool_result",
        "tool_use_id": "tool_2",
        "content": blocks,
    }
    # Identity check: list is passed through, not copied or stringified.
    assert out["content"] is blocks


def test_tool_result_empty_list_passthrough() -> None:
    out = _tool_result("tool_3", [])
    assert out["content"] == []


# --- AnthropicProvider._convert_tool_result_content -------------------------


def _make_anthropic(monkeypatch) -> AnthropicProvider:
    class FakeAnthropicClient:
        def __init__(self, **kwargs: Any) -> None:
            _ = kwargs
            self.messages = SimpleNamespace()

    monkeypatch.setattr(
        "src.provider.anthropic.anthropic.Anthropic", FakeAnthropicClient
    )
    return AnthropicProvider(api_key="test", model="claude-test")


def test_anthropic_tool_result_string_path_unchanged(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch)
    assert provider._convert_tool_result_content("hello") == "hello"


def test_anthropic_tool_result_text_only_list_path_unchanged(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch)
    out = provider._convert_tool_result_content(
        [{"type": "text", "text": "alpha"}, {"type": "text", "text": "beta"}]
    )
    assert out == [
        {"type": "text", "text": "alpha"},
        {"type": "text", "text": "beta"},
    ]


def test_anthropic_tool_result_image_passthrough(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch)
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "AAAA",
        },
    }
    out = provider._convert_tool_result_content(
        [{"type": "text", "text": "see:"}, image_block]
    )
    assert out == [
        {"type": "text", "text": "see:"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAAA",
            },
        },
    ]


def test_anthropic_tool_result_image_non_base64_source_degrades(monkeypatch) -> None:
    """Image with a non-``base64`` source falls back to a stringified text block."""
    provider = _make_anthropic(monkeypatch)
    block = {
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/x.png"},
    }
    out = provider._convert_tool_result_content([block])
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    # Stringified JSON contains the source dump.
    assert "url" in out[0]["text"]


def test_anthropic_tool_result_image_empty_data_degrades(monkeypatch) -> None:
    """Defensive: an image block with empty base64 data falls back to text."""
    provider = _make_anthropic(monkeypatch)
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": ""},
    }
    out = provider._convert_tool_result_content([block])
    assert isinstance(out, list)
    assert out[0]["type"] == "text"


def test_anthropic_full_message_pipeline_with_image() -> None:
    """End-to-end: a user message carrying a multimodal tool_result block flows
    through ``_convert_messages`` with the image source intact."""

    class FakeAnthropicClient:
        def __init__(self, **kwargs: Any) -> None:
            _ = kwargs
            self.messages = SimpleNamespace()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.client = FakeAnthropicClient()
    provider.model = "claude-test"
    from src.provider.base import ThinkingConfig

    provider.thinking_config = ThinkingConfig()

    messages = [
        {"role": "system", "content": "You are BareAgent."},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {"type": "text", "text": "chart:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AAAA",
                            },
                        },
                    ],
                }
            ],
        },
    ]

    system_prompt, converted = provider._convert_messages(messages)
    assert system_prompt == "You are BareAgent."
    assert len(converted) == 1
    tool_result_block = converted[0]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["content"] == [
        {"type": "text", "text": "chart:"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAAA",
            },
        },
    ]


# --- OpenAIProvider._convert_non_assistant_message --------------------------


def _make_openai(monkeypatch) -> OpenAIProvider:
    class FakeOpenAIClient:
        def __init__(self, **kwargs: Any) -> None:
            _ = kwargs

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    return OpenAIProvider(api_key="test", model="gpt-test")


def test_openai_tool_result_string_path_unchanged(monkeypatch) -> None:
    """Regression: string tool_result content keeps the single ``tool`` message
    shape and never spawns an extra user message."""
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "plain text result",
            }
        ],
    )
    assert out == [
        {"role": "tool", "tool_call_id": "call_1", "content": "plain text result"}
    ]


def test_openai_tool_result_text_only_list_does_not_spawn_user_message(
    monkeypatch,
) -> None:
    """Regression: list content with only text blocks emits a single ``tool``
    message — no trailing user message."""
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "beta"},
                ],
            }
        ],
    )
    assert out == [{"role": "tool", "tool_call_id": "call_1", "content": "alpha\nbeta"}]


def test_openai_tool_result_with_image_spawns_follow_up_user_message(
    monkeypatch,
) -> None:
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {"type": "text", "text": "see:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAAA",
                        },
                    },
                ],
            }
        ],
    )
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "call_1", "content": "see:"}
    assert out[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "[Tool returned 1 image(s)]"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
    }


def test_openai_tool_result_image_only_uses_placeholder_for_tool_content(
    monkeypatch,
) -> None:
    """When the tool only returned images (no text), the ``tool`` message body
    gets a placeholder so the API does not reject an empty string."""
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "BBBB",
                        },
                    }
                ],
            }
        ],
    )
    assert len(out) == 2
    assert out[0]["content"] == "[Tool returned image(s); see next message]"
    assert out[1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,BBBB"


def test_openai_tool_result_multiple_images_share_one_user_message(monkeypatch) -> None:
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {"type": "text", "text": "two:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAA1",
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/gif",
                            "data": "AAA2",
                        },
                    },
                ],
            }
        ],
    )
    assert len(out) == 2
    assert out[0]["content"] == "two:"
    user_content = out[1]["content"]
    assert user_content[0] == {"type": "text", "text": "[Tool returned 2 image(s)]"}
    assert [item["image_url"]["url"] for item in user_content[1:]] == [
        "data:image/png;base64,AAA1",
        "data:image/gif;base64,AAA2",
    ]


def test_openai_tool_result_image_with_non_base64_source_falls_back_to_text(
    monkeypatch,
) -> None:
    provider = _make_openai(monkeypatch)
    out = provider._convert_non_assistant_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {"type": "text", "text": "boom"},
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://x/y.png"},
                    },
                ],
            }
        ],
    )
    # No image_blocks emitted -> no follow-up user message.
    assert len(out) == 1
    assert out[0]["role"] == "tool"
    assert "boom" in out[0]["content"]


# --- Integration: MCP handler -> _tool_result -> provider serialization ----


def _fake_manager_with_running(clients: dict[str, MagicMock]) -> MagicMock:
    manager = MagicMock()
    for client in clients.values():
        client.has_capability.return_value = False
    manager.iter_running_clients.side_effect = lambda: iter(clients.items())
    manager.get_client.side_effect = lambda name: clients.get(name)
    _ = ServerStatus  # used to mirror registry test infra
    return manager


def test_integration_mcp_handler_output_feeds_anthropic_provider(monkeypatch) -> None:
    """MCP handler returns ``list[dict]`` → loop wraps it in a tool_result block
    → Anthropic provider serializes the image source intact."""
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "chart", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.return_value = {
        "content": [
            {"type": "text", "text": "rendered"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        ],
        "isError": False,
    }
    manager = _fake_manager_with_running({"viz": client})
    handler = build_mcp_handlers(manager)["mcp__viz__chart"]

    output = handler()
    assert isinstance(output, list)

    tool_result = _tool_result("toolu_1", output)
    assert tool_result["content"] is output

    provider = _make_anthropic(monkeypatch)
    converted = provider._convert_tool_result_content(tool_result["content"])
    assert converted == [
        {"type": "text", "text": "rendered"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAAA",
            },
        },
    ]


# --- PR6: OpenAI Responses-API image lift ---------------------------------


def test_responses_api_tool_result_with_image_lifts_into_user_message(
    monkeypatch,
) -> None:
    """PR6: a ``tool_result`` carrying an image must produce both a
    ``function_call_output`` (text part) and a follow-up Responses-API user
    message holding the ``input_image`` part — same lift logic as the
    chat_completions path, different output shape."""
    provider = _make_openai(monkeypatch)
    out = provider._convert_response_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_42",
                "content": [
                    {"type": "text", "text": "see:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAAA",
                        },
                    },
                ],
            }
        ],
    )
    # function_call_output for the tool, then a user message holding the image.
    assert len(out) == 2
    assert out[0] == {
        "type": "function_call_output",
        "call_id": "call_42",
        "output": "see:",
    }
    image_msg = out[1]
    assert image_msg["type"] == "message"
    assert image_msg["role"] == "user"
    parts = image_msg["content"]
    assert parts[0] == {"type": "input_text", "text": "[Tool returned 1 image(s)]"}
    assert parts[1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AAAA",
    }


def test_responses_api_text_only_tool_result_does_not_spawn_image_message(
    monkeypatch,
) -> None:
    """Regression: a text-only ``list`` tool_result must NOT spawn a follow-up
    user message on the Responses-API path."""
    provider = _make_openai(monkeypatch)
    out = provider._convert_response_message(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [{"type": "text", "text": "ok"}],
            }
        ],
    )
    assert len(out) == 1
    assert out[0]["type"] == "function_call_output"
    assert out[0]["output"] == "ok"


def test_integration_mcp_handler_output_feeds_openai_provider(monkeypatch) -> None:
    """Same handler output is split into ``tool`` + follow-up ``user`` for OpenAI."""
    client = MagicMock()
    client.list_tools.return_value = [
        {"name": "chart", "description": "", "inputSchema": {"type": "object"}}
    ]
    client.call_tool.return_value = {
        "content": [
            {"type": "text", "text": "rendered"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        ],
        "isError": False,
    }
    manager = _fake_manager_with_running({"viz": client})
    handler = build_mcp_handlers(manager)["mcp__viz__chart"]

    output = handler()
    tool_result = _tool_result("call_1", output)

    provider = _make_openai(monkeypatch)
    converted = provider._convert_non_assistant_message("user", [tool_result])
    assert len(converted) == 2
    assert converted[0] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "rendered",
    }
    assert converted[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "[Tool returned 1 image(s)]"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
    }
