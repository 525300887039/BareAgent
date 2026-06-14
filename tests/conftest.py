"""Shared test fixtures for BareAgent."""

from __future__ import annotations

from pathlib import Path

import pytest

from bareagent.lsp import LSPConfig
from bareagent.main import (
    Config,
    DebugConfig,
    PermissionConfig,
    ProviderConfig,
    SubagentConfig,
    TracingConfig,
    UIConfig,
)
from bareagent.mcp import MCPConfig
from bareagent.provider.base import ThinkingConfig


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
        debug=DebugConfig(),
        tracing=TracingConfig(),
        mcp=MCPConfig(),
        lsp=LSPConfig(),
    )


# Fixtures that bind a real localhost ``ThreadingHTTPServer``. Tests using them
# are flaky on local machines (port/timeout sensitivity) and must be excluded
# from the default run — same class as the web-viewer suite. Tests in the same
# files that take no fixture (e.g. ``*_is_transport_subclass``) stay in default.
_SOCKET_SERVER_FIXTURES = {"json_server", "sse_server", "legacy_server"}


def pytest_collection_modifyitems(config, items):
    """Auto-mark unstable/manual tests so they are excluded by default.

    ``*_manual.py`` files, the web-viewer suite, and any test bound to a real
    localhost socket fixture rely on real sockets / external services and are
    flaky on local machines; tag them ``manual`` + ``slow`` so the default
    ``-m 'not manual'`` selection skips them. Run them explicitly with
    ``pytest -m manual``.
    """
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        uses_socket_fixture = bool(
            _SOCKET_SERVER_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        )
        if (
            path.endswith("_manual.py")
            or "test_web_viewer" in path
            or uses_socket_fixture
        ):
            item.add_marker(pytest.mark.manual)
            item.add_marker(pytest.mark.slow)
