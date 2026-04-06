"""Shared test fixtures for BareAgent."""

from __future__ import annotations

from pathlib import Path

from src.main import Config, PermissionConfig, ProviderConfig, SubagentConfig, UIConfig
from src.provider.base import ThinkingConfig


def make_test_config(tmp_path: Path) -> Config:
    """Create a minimal Config for tests that need one."""
    return Config(
        provider=ProviderConfig(
            name="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        ),
        permission=PermissionConfig(mode="default", allow=[], deny=[]),
        ui=UIConfig(stream=False, theme="catppuccin-mocha"),
        subagent=SubagentConfig(max_depth=3, default_type="general-purpose"),
        thinking=ThinkingConfig(),
        path=tmp_path / "config.toml",
    )
