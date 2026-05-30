"""Tests for provider key resolution and preset-driven routing in factory."""

from __future__ import annotations

import dataclasses

import pytest

from src.provider import factory
from src.provider.presets import resolve_preset
from tests.conftest import make_test_config


class FakeOpenAIProvider:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeAnthropicProvider:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@pytest.fixture
def patched_factory(monkeypatch):
    """Swap real provider classes for capturing fakes."""
    monkeypatch.setattr(factory, "OpenAIProvider", FakeOpenAIProvider)
    monkeypatch.setattr(factory, "AnthropicProvider", FakeAnthropicProvider)


def _with_provider(config, **overrides):
    config.provider = dataclasses.replace(config.provider, **overrides)
    return config


def test_create_provider_prefers_explicit_api_key(tmp_path, monkeypatch, patched_factory) -> None:
    # Simulate a qwen-style key with no ``sk-`` prefix and ensure the named env
    # var is absent so a fallback would fail loudly if it were taken.
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config = _with_provider(
        make_test_config(tmp_path),
        name="qwen",
        model="qwen-plus",
        api_key_env="DASHSCOPE_API_KEY",
        api_key="abc123def",
    )

    provider = factory.create_provider(config)

    assert isinstance(provider, FakeOpenAIProvider)
    assert provider.kwargs["api_key"] == "abc123def"


def test_create_provider_falls_back_to_api_key_env(tmp_path, monkeypatch, patched_factory) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-secret")
    config = _with_provider(
        make_test_config(tmp_path),
        name="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        api_key=None,
    )

    provider = factory.create_provider(config)

    assert provider.kwargs["api_key"] == "env-secret"


def test_create_provider_raises_when_no_key_available(
    tmp_path, monkeypatch, patched_factory
) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="openai",
        model="gpt-4o",
        api_key_env="",
        api_key=None,
    )

    with pytest.raises(ValueError, match="missing both 'api_key' and 'api_key_env'"):
        factory.create_provider(config)


def test_create_provider_raises_when_env_var_unset(tmp_path, monkeypatch, patched_factory) -> None:
    monkeypatch.delenv("MISSING_KEY_ENV", raising=False)
    config = _with_provider(
        make_test_config(tmp_path),
        name="openai",
        model="gpt-4o",
        api_key_env="MISSING_KEY_ENV",
        api_key=None,
    )

    with pytest.raises(ValueError, match="MISSING_KEY_ENV"):
        factory.create_provider(config)


def test_create_provider_routes_qwen_to_openai_with_preset_base_url(
    tmp_path, patched_factory
) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="qwen",
        model="qwen-plus",
        api_key="key",
        base_url=None,
    )

    provider = factory.create_provider(config)

    assert isinstance(provider, FakeOpenAIProvider)
    assert provider.kwargs["base_url"] == resolve_preset("qwen").default_base_url


def test_create_provider_routes_glm_to_openai_with_preset_base_url(
    tmp_path, patched_factory
) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="glm",
        model="glm-4.6",
        api_key="key",
        base_url=None,
    )

    provider = factory.create_provider(config)

    assert isinstance(provider, FakeOpenAIProvider)
    assert provider.kwargs["base_url"] == resolve_preset("glm").default_base_url


def test_create_provider_explicit_base_url_overrides_preset(tmp_path, patched_factory) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="qwen",
        model="qwen-plus",
        api_key="key",
        base_url="https://custom.example/v1",
    )

    provider = factory.create_provider(config)

    assert provider.kwargs["base_url"] == "https://custom.example/v1"


def test_create_provider_routes_anthropic(tmp_path, patched_factory) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="anthropic",
        model="claude-sonnet-4-20250514",
        api_key="key",
    )

    provider = factory.create_provider(config)

    assert isinstance(provider, FakeAnthropicProvider)


def test_create_provider_unknown_name_raises(tmp_path, patched_factory) -> None:
    config = _with_provider(
        make_test_config(tmp_path),
        name="not-a-real-provider",
        api_key="key",
    )

    with pytest.raises(ValueError, match="Unknown provider"):
        factory.create_provider(config)
