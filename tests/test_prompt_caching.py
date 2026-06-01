from __future__ import annotations

import copy
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src.main import _parse_cache_config, load_config
from src.memory.token_tracker import TokenTracker, resolve_cache_multipliers
from src.provider import factory
from src.provider.anthropic import AnthropicProvider
from src.provider.base import CacheConfig, ThinkingConfig
from src.provider.openai import OpenAIProvider


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_anthropic(monkeypatch, cache_config: CacheConfig | None) -> AnthropicProvider:
    class FakeAnthropicClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self.messages = SimpleNamespace()

    monkeypatch.setattr("src.provider.anthropic.anthropic.Anthropic", FakeAnthropicClient)
    return AnthropicProvider(api_key="test", model="claude-opus-4-8", cache_config=cache_config)


def _make_openai(monkeypatch, **kwargs) -> OpenAIProvider:
    class FakeOpenAIClient:
        def __init__(self, **client_kwargs) -> None:
            _ = client_kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("src.provider.openai.openai.OpenAI", FakeOpenAIClient)
    return OpenAIProvider(api_key="test", model=kwargs.pop("model", "gpt-4o"), **kwargs)


def _count_cache_controls(params: dict) -> int:
    count = 0
    for tool in params.get("tools", []):
        if "cache_control" in tool:
            count += 1
    system = params.get("system")
    if isinstance(system, list):
        count += sum(1 for block in system if "cache_control" in block)
    for message in params.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1 for block in content if isinstance(block, dict) and "cache_control" in block
            )
    return count


_TOOLS = [
    {"name": "read_file", "description": "read", "parameters": {"type": "object"}},
    {"name": "bash", "description": "run", "parameters": {"type": "object"}},
]


@dataclass
class _CacheResponse:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


# --------------------------------------------------------------------------- #
# Anthropic breakpoint injection
# --------------------------------------------------------------------------- #
def test_caching_disabled_keeps_bare_string_system_and_no_breakpoints(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=None)
    messages = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "Hello"},
    ]

    params = provider._build_request_params(messages, _TOOLS)

    # Byte-identical to the pre-caching shape: bare string system, plain tools,
    # plain string message content, zero cache_control breakpoints.
    assert params["system"] == "You are a helpful agent."
    assert all("cache_control" not in tool for tool in params["tools"])
    assert params["messages"][-1]["content"] == "Hello"
    assert _count_cache_controls(params) == 0


def test_caching_disabled_via_enabled_false(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=False))
    params = provider._build_request_params(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], _TOOLS
    )
    assert isinstance(params["system"], str)
    assert _count_cache_controls(params) == 0


def test_caching_enabled_injects_tools_system_and_conversation_breakpoints(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True))
    messages = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "Hello"},
    ]

    params = provider._build_request_params(messages, _TOOLS)

    # system becomes a content-block list with a breakpoint on the last block.
    assert isinstance(params["system"], list)
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert params["system"][-1]["text"] == "You are a helpful agent."
    # last tool carries a breakpoint; earlier tools do not.
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # the last message's string content is lifted to a text block with a breakpoint.
    last_content = params["messages"][-1]["content"]
    assert last_content == [
        {"type": "text", "text": "Hello", "cache_control": {"type": "ephemeral"}}
    ]
    # tools + system + conversation == 3, always within Anthropic's max of 4.
    assert _count_cache_controls(params) == 3
    assert _count_cache_controls(params) <= 4


def test_caching_ttl_1h_shape(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True, ttl="1h"))
    params = provider._build_request_params(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], _TOOLS
    )
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_caching_conversation_breakpoint_attaches_to_last_list_block(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True))
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "output",
                }
            ],
        },
    ]

    params = provider._build_request_params(messages, _TOOLS)

    last_block = params["messages"][-1]["content"][-1]
    assert last_block["type"] == "tool_result"
    assert last_block["cache_control"] == {"type": "ephemeral"}


def test_caching_skips_breakpoint_on_trailing_thinking_block(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True))
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "thinking", "thinking": "reasoning", "signature": "sig_1"},
            ],
        },
    ]

    params = provider._build_request_params(messages, _TOOLS)

    # A thinking block must not carry cache_control; the conversation breakpoint
    # is skipped, leaving only tools + system (== 2).
    last_blocks = params["messages"][-1]["content"]
    assert all("cache_control" not in block for block in last_blocks)
    assert _count_cache_controls(params) == 2


def test_caching_does_not_mutate_caller_messages(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True))
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]
    original = copy.deepcopy(messages)

    provider._build_request_params(messages, _TOOLS)

    assert messages == original


# --------------------------------------------------------------------------- #
# Anthropic usage parsing
# --------------------------------------------------------------------------- #
def test_anthropic_parse_response_reads_cache_usage(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=CacheConfig(enabled=True))
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=5,
            output_tokens=3,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=200,
        ),
    )

    parsed = provider._parse_response(response)

    assert parsed.input_tokens == 5
    assert parsed.cache_creation_input_tokens == 100
    assert parsed.cache_read_input_tokens == 200


def test_anthropic_parse_response_cache_fields_default_zero(monkeypatch) -> None:
    provider = _make_anthropic(monkeypatch, cache_config=None)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
    )

    parsed = provider._parse_response(response)

    assert parsed.cache_creation_input_tokens == 0
    assert parsed.cache_read_input_tokens == 0


# --------------------------------------------------------------------------- #
# OpenAI / DeepSeek usage normalization
# --------------------------------------------------------------------------- #
def test_openai_parse_response_normalizes_cached_tokens(monkeypatch) -> None:
    provider = _make_openai(monkeypatch)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=13,
            completion_tokens=6,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        ),
    )

    parsed = provider._parse_response(response)

    # prompt_tokens includes cached -> full-price input is the remainder.
    assert parsed.input_tokens == 3
    assert parsed.cache_read_input_tokens == 10
    assert parsed.cache_creation_input_tokens == 0


def test_deepseek_parse_response_normalizes_cache_hit_tokens(monkeypatch) -> None:
    provider = _make_openai(monkeypatch, model="deepseek-chat")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=13,
            completion_tokens=6,
            prompt_cache_hit_tokens=8,
            prompt_cache_miss_tokens=5,
        ),
    )

    parsed = provider._parse_response(response)

    assert parsed.input_tokens == 5
    assert parsed.cache_read_input_tokens == 8


def test_openai_parse_response_without_cache_fields_is_unchanged(monkeypatch) -> None:
    provider = _make_openai(monkeypatch)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=13, completion_tokens=6),
    )

    parsed = provider._parse_response(response)

    assert parsed.input_tokens == 13
    assert parsed.cache_read_input_tokens == 0


def test_openai_responses_api_normalizes_cached_tokens(monkeypatch) -> None:
    provider = _make_openai(monkeypatch, wire_api="responses")
    payload = {
        "status": "completed",
        "usage": {
            "input_tokens": 13,
            "output_tokens": 6,
            "input_tokens_details": {"cached_tokens": 10},
        },
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            }
        ],
    }

    parsed = provider._parse_responses_api_response(payload)

    assert parsed.input_tokens == 3
    assert parsed.cache_read_input_tokens == 10


# --------------------------------------------------------------------------- #
# TokenTracker cache accounting
# --------------------------------------------------------------------------- #
def test_resolve_cache_multipliers_by_family() -> None:
    assert resolve_cache_multipliers("claude-opus-4-8") == (0.1, 1.25)
    assert resolve_cache_multipliers("gpt-4o") == (0.5, 0.0)
    assert resolve_cache_multipliers("o3-mini") == (0.5, 0.0)
    assert resolve_cache_multipliers("deepseek-chat") == (0.1, 0.0)
    # Unknown family -> conservative Anthropic-like fallback.
    assert resolve_cache_multipliers("mystery-model") == (0.1, 1.25)


def test_record_accumulates_cache_tokens() -> None:
    tracker = TokenTracker()
    tracker.record(
        _CacheResponse(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=50,
        ),
        "claude-opus-4-8",
    )

    assert tracker.total_cache_read == 100
    assert tracker.total_cache_write == 50
    assert tracker.total_tokens == 10 + 4 + 100 + 50
    usage = tracker.per_model["claude-opus-4-8"]
    assert usage.cache_read_tokens == 100
    assert usage.cache_write_tokens == 50


def test_estimate_cost_applies_cache_multipliers() -> None:
    tracker = TokenTracker()
    # claude-opus-4 built-in input price = 15 USD / 1M tokens.
    tracker.record(
        _CacheResponse(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_input_tokens=1_000_000,
            cache_creation_input_tokens=1_000_000,
        ),
        "claude-opus-4-8",
    )

    cost = tracker.estimate_cost(None)

    # input 1M*15 + read 1M*15*0.1 + write 1M*15*1.25
    assert cost == pytest.approx(15.0 + 1.5 + 18.75)


def test_reset_clears_cache_totals() -> None:
    tracker = TokenTracker()
    tracker.record(
        _CacheResponse(input_tokens=1, output_tokens=1, cache_read_input_tokens=5),
        "claude-opus-4-8",
    )
    tracker.reset()
    assert tracker.total_cache_read == 0
    assert tracker.total_cache_write == 0
    assert tracker.per_model == {}


def test_summary_shows_cache_line_when_present() -> None:
    tracker = TokenTracker()
    tracker.record(
        _CacheResponse(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=50,
        ),
        "claude-opus-4-8",
    )

    out = tracker.summary(None)

    assert "Cache:" in out
    assert "100 read" in out
    assert "50 write" in out
    assert "cache-read" in out


def test_summary_omits_cache_line_without_cache_activity() -> None:
    tracker = TokenTracker()
    tracker.record(_CacheResponse(input_tokens=10, output_tokens=4), "model-a")

    out = tracker.summary(None)

    assert "Cache:" not in out


# --------------------------------------------------------------------------- #
# Config parsing + factory threading
# --------------------------------------------------------------------------- #
def test_parse_cache_config_defaults() -> None:
    cfg = _parse_cache_config({})
    assert cfg.enabled is True
    assert cfg.ttl == "5m"


def test_parse_cache_config_explicit() -> None:
    cfg = _parse_cache_config({"enabled": False, "ttl": "1h"})
    assert cfg.enabled is False
    assert cfg.ttl == "1h"


def test_parse_cache_config_invalid_ttl_falls_back() -> None:
    assert _parse_cache_config({"ttl": "9h"}).ttl == "5m"


def test_parse_cache_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BAREAGENT_CACHE_ENABLED", "false")
    assert _parse_cache_config({"enabled": True}).enabled is False


def test_load_config_has_cache_default(tmp_path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert isinstance(cfg.cache, CacheConfig)
    assert cfg.cache.enabled is True
    assert cfg.cache.ttl == "5m"


def test_load_config_parses_cache_section(tmp_path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        '[cache]\nenabled = false\nttl = "1h"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.cache.enabled is False
    assert cfg.cache.ttl == "1h"


def test_build_cache_config_none_defaults_enabled() -> None:
    assert factory._build_cache_config(None) == CacheConfig(enabled=True, ttl="5m")


def test_build_cache_config_invalid_ttl_falls_back() -> None:
    assert factory._build_cache_config({"ttl": "bogus"}).ttl == "5m"


def test_factory_passes_cache_config_to_anthropic(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnthropicProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setattr(factory, "AnthropicProvider", FakeAnthropicProvider)

    config = SimpleNamespace(
        provider=SimpleNamespace(
            name="anthropic",
            model="claude-opus-4-8",
            api_key_env="ANTHROPIC_API_KEY",
            base_url=None,
        ),
        thinking=ThinkingConfig(),
        cache=CacheConfig(enabled=False, ttl="1h"),
    )

    factory.create_provider(config)

    assert captured["cache_config"] == CacheConfig(enabled=False, ttl="1h")
