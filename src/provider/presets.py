from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProviderRoute = Literal["anthropic", "openai"]


@dataclass(slots=True, frozen=True)
class ProviderPreset:
    """Static configuration for a known provider channel.

    Drives both the factory (route + default base_url/key env) and, later, the
    interactive setup wizard (display_name + candidate_models). Plain immutable
    data, looked up by id via :func:`resolve_preset` -- no dynamic registry.
    """

    id: str
    display_name: str
    route: ProviderRoute
    default_base_url: str | None
    default_api_key_env: str
    candidate_models: tuple[str, ...] = field(default_factory=tuple)


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "anthropic": ProviderPreset(
        id="anthropic",
        display_name="Claude (Anthropic)",
        route="anthropic",
        default_base_url=None,
        default_api_key_env="ANTHROPIC_API_KEY",
        candidate_models=("claude-sonnet-4-20250514", "claude-opus-4-20250514"),
    ),
    "openai": ProviderPreset(
        id="openai",
        display_name="ChatGPT (OpenAI)",
        route="openai",
        default_base_url=None,
        default_api_key_env="OPENAI_API_KEY",
        candidate_models=("gpt-4.1", "gpt-4o"),
    ),
    "deepseek": ProviderPreset(
        id="deepseek",
        display_name="DeepSeek",
        route="openai",
        default_base_url="https://api.deepseek.com",
        default_api_key_env="DEEPSEEK_API_KEY",
        candidate_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "qwen": ProviderPreset(
        id="qwen",
        display_name="Qwen (DashScope)",
        route="openai",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_api_key_env="DASHSCOPE_API_KEY",
        candidate_models=("qwen-plus", "qwen-max", "qwen-turbo"),
    ),
    "glm": ProviderPreset(
        id="glm",
        display_name="GLM (Zhipu/BigModel)",
        route="openai",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_api_key_env="ZHIPUAI_API_KEY",
        candidate_models=("glm-4.6", "glm-4-plus"),
    ),
}


def resolve_preset(preset_id: str) -> ProviderPreset | None:
    """Look up a provider preset by id (case-insensitive), or None if unknown."""
    return PROVIDER_PRESETS.get(preset_id.strip().lower())
