from __future__ import annotations

# Input-modality capabilities.
#
# Vision (image_in) is a *model* property, not a provider one: the same
# OpenAIProvider serves both gpt-4o (vision) and deepseek-chat (text-only). So
# unlike ``BaseLLMProvider.cache_mode`` (a provider ClassVar), this is a static
# model-prefix table plus a config/env override -- the presets.py spirit applied
# to models, aligned with kimi-cli's KIMI_MODEL_CAPABILITIES.
#
# The table is living knowledge and will drift as models ship; the authoritative
# escape hatch is the override ([capabilities] image_in / BAREAGENT_MODEL_IMAGE_IN)
# for models not yet listed. See token_tracker.DEFAULT_PRICES for the same
# "table may be stale, override wins" posture.

# Known model-id prefixes that accept image input. Matched case-insensitively
# against the start of the model string.
KNOWN_IMAGE_INPUT_PREFIXES: tuple[str, ...] = (
    # Anthropic: Claude 3 onward is vision-capable across the family.
    "claude-3",
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-4",
    # OpenAI: 4o / 4.1 / 4-turbo / 4-vision / 5 and the o-series reasoning models.
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    # Google Gemini: 1.5 and 2.x are multimodal.
    "gemini-1.5",
    "gemini-2",
    # Explicit vision variants for Qwen / GLM / DeepSeek.
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "glm-4v",
    "glm-4.6v",
    "deepseek-vl",
)

# Model-id prefixes that accept native base64 PDF *document* blocks. Model
# support is only half of the gate: the provider transport must also declare
# ``native_pdf_input`` because OpenAI-compatible endpoints cannot serialize the
# Anthropic-native block shape, even when their model id starts with "claude".
KNOWN_PDF_INPUT_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-4",
)

# Future extension point: video_in / audio_in would each get their own prefix
# table + resolver here. Not built until needed (YAGNI).


def supports_image_input(model: str, *, override: bool | None = None) -> bool:
    """Whether *model* accepts image input.

    Precedence: an explicit ``override`` (True/False) wins; otherwise a prefix
    match against the known-vision table. Unknown models default to ``False``
    (fail-safe) so an image is never sent to a model that would reject it at the
    API -- the caller surfaces a friendly error pointing at the override.
    """
    if override is not None:
        return override
    normalized = (model or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in KNOWN_IMAGE_INPUT_PREFIXES)


def supports_pdf_input(
    model: str,
    *,
    provider_supports_pdf: bool,
    override: bool | None = None,
) -> bool:
    """Whether *model* accepts native base64 PDF document blocks.

    The provider transport gate is mandatory and cannot be overridden. Within
    a supported provider, an explicit ``override`` wins over the Claude model
    table. Unknown models default to ``False`` so read_file falls back to pypdf
    text instead of sending a document block the model may reject.
    """
    if not provider_supports_pdf:
        return False
    if override is not None:
        return override
    normalized = (model or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in KNOWN_PDF_INPUT_PREFIXES)
