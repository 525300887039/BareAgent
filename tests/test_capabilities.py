from __future__ import annotations

import pytest

from bareagent.provider.capabilities import supports_image_input, supports_pdf_input


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-5-sonnet-20241022",
        "claude-opus-4-8",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4-turbo",
        "gpt-5",
        "o1-preview",
        "o3-mini",
        "o4",
        "gemini-1.5-pro",
        "gemini-2.5-pro",
        "qwen-vl-max",
        "qwen2.5-vl-72b",
        "glm-4v",
        "deepseek-vl-7b",
        "CLAUDE-OPUS-4-8",  # case-insensitive
    ],
)
def test_known_vision_models_allowed(model: str) -> None:
    assert supports_image_input(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "qwen-plus",
        "qwen-max",
        "glm-4.6",
        "gpt-3.5-turbo",
        "text-embedding-3-small",
        "some-future-unknown-model",
        "",
    ],
)
def test_unknown_or_text_models_denied_by_default(model: str) -> None:
    assert supports_image_input(model) is False


def test_override_true_forces_allow_even_for_unknown() -> None:
    assert supports_image_input("some-unknown-model", override=True) is True
    assert supports_image_input("deepseek-chat", override=True) is True


def test_override_false_forces_deny_even_for_known_vision() -> None:
    assert supports_image_input("gpt-4o", override=False) is False
    assert supports_image_input("claude-opus-4-8", override=False) is False


def test_override_none_falls_back_to_table() -> None:
    assert supports_image_input("gpt-4o", override=None) is True
    assert supports_image_input("deepseek-chat", override=None) is False


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-5-sonnet-20241022",
        "claude-opus-4-8",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
        "CLAUDE-OPUS-4-8",  # case-insensitive
    ],
)
def test_claude_models_allow_native_pdf(model: str) -> None:
    assert supports_pdf_input(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",  # vision-capable but not native PDF (Anthropic-only feature)
        "gemini-2.5-pro",
        "deepseek-chat",
        "some-future-unknown-model",
        "",
    ],
)
def test_non_claude_models_denied_native_pdf(model: str) -> None:
    assert supports_pdf_input(model) is False


def test_pdf_override_forces_regardless_of_table() -> None:
    assert supports_pdf_input("gpt-4o", override=True) is True
    assert supports_pdf_input("claude-opus-4-8", override=False) is False
    assert supports_pdf_input("gpt-4o", override=None) is False
