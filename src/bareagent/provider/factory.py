from __future__ import annotations

import logging
import os
from typing import Any, Literal, cast

from bareagent.provider.anthropic import AnthropicProvider
from bareagent.provider.base import (
    VALID_CACHE_TTLS,
    VALID_THINKING_MODES,
    BaseLLMProvider,
    CacheConfig,
    ThinkingConfig,
)
from bareagent.provider.openai import OpenAIProvider
from bareagent.provider.presets import resolve_preset


def _resolve_api_key(provider_config: Any) -> str:
    """Resolve the API key, preferring an explicit plaintext key.

    Priority: ``provider_config.api_key`` (explicit plaintext, used as-is) ->
    ``provider_config.api_key_env`` (an ``sk-`` value is treated as plaintext,
    otherwise it names an environment variable). Fixes non-``sk-`` prefixed
    keys (qwen/glm) being misread as env var names.
    """
    explicit_key = getattr(provider_config, "api_key", None)
    if explicit_key:
        return str(explicit_key)

    api_key_env = getattr(provider_config, "api_key_env", "")
    if not api_key_env:
        raise ValueError(
            "Provider config is missing both 'api_key' and 'api_key_env'. "
            "Please provide the API key directly via 'api_key', or specify the "
            "environment variable name that holds it via 'api_key_env'."
        )
    if api_key_env.startswith("sk-"):
        return api_key_env
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise ValueError(f"Missing API key in environment variable: {api_key_env}")
    return api_key


def create_provider(config: Any) -> BaseLLMProvider:
    provider_config = getattr(config, "provider", None)
    if provider_config is None:
        raise ValueError("Config is missing a provider section.")

    provider_name = str(getattr(provider_config, "name", "")).strip().lower()
    model = getattr(provider_config, "model", "")
    api_key = _resolve_api_key(provider_config)

    preset = resolve_preset(provider_name)
    if preset is None:
        raise ValueError(f"Unknown provider: {provider_name}")

    if preset.route == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=model,
            thinking_config=_build_thinking_config(getattr(config, "thinking", None)),
            cache_config=_build_cache_config(getattr(config, "cache", None)),
        )

    base_url = getattr(provider_config, "base_url", None) or preset.default_base_url
    return OpenAIProvider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        wire_api=getattr(provider_config, "wire_api", None),
    )


def _validated_thinking_mode(mode: str) -> Literal["enabled", "adaptive", "disabled"]:
    if mode not in VALID_THINKING_MODES:
        logging.warning("Invalid thinking mode %r, falling back to 'adaptive'", mode)
        return "adaptive"
    return cast(Literal["enabled", "adaptive", "disabled"], mode)


def _validated_cache_ttl(ttl: str) -> Literal["5m", "1h"]:
    if ttl not in VALID_CACHE_TTLS:
        logging.warning("Invalid cache ttl %r, falling back to '5m'", ttl)
        return "5m"
    return cast(Literal["5m", "1h"], ttl)


def _build_cache_config(raw_config: Any) -> CacheConfig:
    """Coerce a config-supplied cache section into a :class:`CacheConfig`.

    ``None`` (e.g. a namespace without a cache attribute) yields the default
    enabled instance so the app defaults to caching ON.
    """
    if raw_config is None:
        return CacheConfig()
    if isinstance(raw_config, CacheConfig):
        return CacheConfig(
            enabled=bool(raw_config.enabled),
            ttl=_validated_cache_ttl(raw_config.ttl),
        )
    if isinstance(raw_config, dict):
        return CacheConfig(
            enabled=bool(raw_config.get("enabled", True)),
            ttl=_validated_cache_ttl(str(raw_config.get("ttl", "5m"))),
        )
    return CacheConfig(
        enabled=bool(getattr(raw_config, "enabled", True)),
        ttl=_validated_cache_ttl(str(getattr(raw_config, "ttl", "5m"))),
    )


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
