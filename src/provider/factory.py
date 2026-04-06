from __future__ import annotations

import logging
import os
from typing import Any

from src.provider.anthropic import AnthropicProvider
from src.provider.base import BaseLLMProvider, ThinkingConfig
from src.provider.openai import OpenAIProvider

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_VALID_THINKING_MODES = {"enabled", "adaptive", "disabled"}


def create_provider(config: Any) -> BaseLLMProvider:
    provider_config = getattr(config, "provider", None)
    if provider_config is None:
        raise ValueError("Config is missing a provider section.")

    provider_name = str(getattr(provider_config, "name", "")).strip().lower()
    model = getattr(provider_config, "model", "")
    api_key_env = getattr(provider_config, "api_key_env", "")
    if not api_key_env:
        raise ValueError(
            "Provider config is missing 'api_key_env'. "
            "Please specify the environment variable name that holds the API key."
        )
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise ValueError(f"Missing API key in environment variable: {api_key_env}")

    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=model,
            thinking_config=_build_thinking_config(getattr(config, "thinking", None)),
        )
    if provider_name == "openai":
        return OpenAIProvider(
            api_key=api_key,
            model=model,
            base_url=getattr(provider_config, "base_url", None),
            wire_api=getattr(provider_config, "wire_api", None),
        )
    if provider_name == "deepseek":
        return OpenAIProvider(
            api_key=api_key,
            model=model,
            base_url=getattr(provider_config, "base_url", None) or DEEPSEEK_BASE_URL,
            wire_api=getattr(provider_config, "wire_api", None),
        )

    raise ValueError(f"Unknown provider: {provider_name}")


def _validated_thinking_mode(mode: str) -> str:
    if mode not in _VALID_THINKING_MODES:
        logging.warning("Invalid thinking mode %r, falling back to 'adaptive'", mode)
        return "adaptive"
    return mode


def _build_thinking_config(raw_config: Any) -> ThinkingConfig:
    if raw_config is None:
        return ThinkingConfig()
    if isinstance(raw_config, ThinkingConfig):
        return ThinkingConfig(
            mode=_validated_thinking_mode(raw_config.mode),
            budget_tokens=raw_config.budget_tokens,
        )
    if isinstance(raw_config, dict):
        return ThinkingConfig(
            mode=_validated_thinking_mode(str(raw_config.get("mode", "adaptive"))),
            budget_tokens=int(raw_config.get("budget_tokens", 10000)),
        )
    return ThinkingConfig(
        mode=_validated_thinking_mode(str(getattr(raw_config, "mode", "adaptive"))),
        budget_tokens=int(getattr(raw_config, "budget_tokens", 10000)),
    )
