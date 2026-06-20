"""Tests for the REPL ``/mcp:<server>:<prompt>`` routing in src.main."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from bareagent.main import _dispatch_mcp_prompt, _parse_mcp_prompt_command
from bareagent.mcp.errors import MCPCallError


def _make_console() -> MagicMock:
    """Minimal stand-in for AgentConsole — only the methods _dispatch needs."""
    console = MagicMock()
    console.print_error = MagicMock()
    console.print_status = MagicMock()
    return console


def _make_manager(
    server_name: str,
    *,
    client: MagicMock | None = None,
) -> MagicMock:
    manager = MagicMock()
    manager.get_client.side_effect = lambda name: client if name == server_name else None
    return manager


def _make_client(
    *,
    has_prompts: bool = True,
    prompt_result: dict[str, Any] | None = None,
    prompt_error: Exception | None = None,
) -> MagicMock:
    client = MagicMock()
    client.has_capability.side_effect = lambda name: name == "prompts" and has_prompts
    if prompt_error is not None:
        client.get_prompt.side_effect = prompt_error
    else:
        client.get_prompt.return_value = prompt_result or {"messages": []}
    return client


# --- _parse_mcp_prompt_command -------------------------------------------


def test_parser_extracts_server_prompt_and_kwargs() -> None:
    parsed = _parse_mcp_prompt_command("/mcp:fetch:summarize url=https://x.com depth=3")
    assert parsed == ("fetch", "summarize", {"url": "https://x.com", "depth": "3"})


def test_parser_returns_none_when_colon_separator_missing() -> None:
    assert _parse_mcp_prompt_command("/mcp:fetch") is None


def test_parser_returns_none_when_prompt_name_empty() -> None:
    assert _parse_mcp_prompt_command("/mcp:fetch:") is None


def test_parser_returns_none_when_server_name_empty() -> None:
    assert _parse_mcp_prompt_command("/mcp::summarize") is None


def test_parser_returns_none_for_non_mcp_prefix() -> None:
    assert _parse_mcp_prompt_command("/help") is None


def test_parser_skips_non_kv_tokens_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="bareagent.main"):
        parsed = _parse_mcp_prompt_command("/mcp:fetch:summarize url=https://x.com loose depth=3")
    assert parsed == ("fetch", "summarize", {"url": "https://x.com", "depth": "3"})
    assert any("loose" in rec.getMessage() for rec in caplog.records)


def test_parser_skips_empty_key_token_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="bareagent.main"):
        parsed = _parse_mcp_prompt_command("/mcp:fetch:summarize =bad ok=yes")
    assert parsed == ("fetch", "summarize", {"ok": "yes"})
    assert any("empty key" in rec.getMessage() for rec in caplog.records)


def test_parser_accepts_no_arguments() -> None:
    assert _parse_mcp_prompt_command("/mcp:fetch:summarize") == (
        "fetch",
        "summarize",
        {},
    )


# --- _dispatch_mcp_prompt -------------------------------------------------


def test_dispatch_reports_usage_on_malformed_command() -> None:
    manager = _make_manager("fetch")
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is False
    assert messages == []
    console.print_error.assert_called_once()
    assert "Usage" in console.print_error.call_args.args[0]


def test_dispatch_reports_when_server_not_running() -> None:
    manager = _make_manager("other")  # only "other" exists, "fetch" returns None
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is False
    assert messages == []
    console.print_error.assert_called_once()
    assert "not running" in console.print_error.call_args.args[0]


def test_dispatch_reports_when_server_lacks_prompts_capability() -> None:
    client = _make_client(has_prompts=False)
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is False
    assert messages == []
    client.get_prompt.assert_not_called()
    console.print_error.assert_called_once()
    assert "does not support prompts" in console.print_error.call_args.args[0]


def test_dispatch_injects_messages_on_success() -> None:
    client = _make_client(
        prompt_result={
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Summarize https://x.com",
                    },
                }
            ]
        }
    )
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize url=https://x.com",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is True
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Summarize https://x.com"
    client.get_prompt.assert_called_once_with("summarize", {"url": "https://x.com"})


def test_dispatch_flattens_multi_block_content_array() -> None:
    client = _make_client(
        prompt_result={
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "image", "data": "x"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ]
        }
    )
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is True
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "first\n[image omitted: PR5]\nsecond"


def test_dispatch_does_not_modify_transcript_on_mcp_call_error() -> None:
    client = _make_client(prompt_error=MCPCallError("MCP Error: -32602 missing arg"))
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = [{"role": "system", "content": "baseline"}]

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is False
    assert messages == [{"role": "system", "content": "baseline"}]
    console.print_error.assert_called_once()
    assert "MCP Error: -32602 missing arg" in console.print_error.call_args.args[0]


def test_dispatch_reports_when_messages_array_empty() -> None:
    client = _make_client(prompt_result={"messages": []})
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is False
    assert messages == []
    console.print_error.assert_called_once()


def test_dispatch_accepts_plain_string_content() -> None:
    client = _make_client(
        prompt_result={
            "messages": [
                {"role": "user", "content": "Summarize this."},
            ]
        }
    )
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is True
    assert messages[0]["content"] == "Summarize this."


def test_dispatch_skips_messages_with_unknown_role() -> None:
    client = _make_client(
        prompt_result={
            "messages": [
                {"role": "system", "content": "ignore me"},
                {"role": "user", "content": "keep me"},
            ]
        }
    )
    manager = _make_manager("fetch", client=client)
    console = _make_console()
    messages: list[dict[str, Any]] = []

    appended = _dispatch_mcp_prompt(
        "/mcp:fetch:summarize",
        mcp_manager=manager,
        messages=messages,
        ui_console=console,
    )

    assert appended is True
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "keep me"
