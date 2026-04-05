"""十、Provider 抽象验证

适配：create_provider(config) 接受 config 对象（需有 provider 和 thinking 属性）。
需要设置环境变量才能创建 provider，这里用 monkeypatch 模拟。
"""
from types import SimpleNamespace

import pytest

from src.provider.factory import create_provider
from src.provider.base import BaseLLMProvider


def _make_config(name, model, api_key_env):
    return SimpleNamespace(
        provider=SimpleNamespace(
            name=name, model=model, api_key_env=api_key_env,
            base_url=None, wire_api=None,
        ),
        thinking=SimpleNamespace(mode="disabled", budget_tokens=1000),
    )


def test_factory_creates_openai(monkeypatch):
    """工厂应能创建 OpenAI provider"""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-fake")
    provider = create_provider(_make_config("openai", "gpt-4.1", "OPENAI_API_KEY"))
    assert isinstance(provider, BaseLLMProvider)


def test_factory_creates_anthropic(monkeypatch):
    """工厂应能创建 Anthropic provider"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-fake")
    provider = create_provider(_make_config("anthropic", "claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"))
    assert isinstance(provider, BaseLLMProvider)


def test_factory_invalid_provider(monkeypatch):
    """无效 provider 名称应抛出异常"""
    monkeypatch.setenv("X_KEY", "test-key-fake")
    with pytest.raises(ValueError, match="Unknown provider"):
        create_provider(_make_config("nonexistent_provider", "x", "X_KEY"))
