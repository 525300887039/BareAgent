"""End-to-end smoke tests against a real ``mcp-server-fetch`` (stdio).

Manual: requires ``uvx`` on PATH and network access to ``https://example.com``.
The ``_manual.py`` suffix excludes the module from the default ``pytest`` run
(project convention).
"""

from __future__ import annotations

import shutil

import pytest

from bareagent.mcp import MCPConfig, MCPManager, MCPServerConfig
from bareagent.mcp.registry import build_mcp_handlers


def _require_uvx() -> None:
    """Skip the module when ``uvx`` is not available locally."""
    if shutil.which("uvx") is None:
        pytest.skip("uvx not installed; skip mcp-server-fetch E2E", allow_module_level=False)


@pytest.fixture()
def fetch_manager():
    _require_uvx()
    cfg = MCPConfig(
        servers=[
            MCPServerConfig(
                name="fetch",
                transport="stdio",
                command=["uvx"],
                args=["mcp-server-fetch"],
                start_timeout=30.0,
            )
        ],
        start_timeout=30.0,
    )
    manager = MCPManager(cfg)
    manager.start_all()
    try:
        yield manager
    finally:
        manager.close_all()


def test_fetch_handshake(fetch_manager: MCPManager) -> None:
    """The fetch server must finish handshake and expose at least one tool."""
    running = list(fetch_manager.iter_running_clients())
    assert any(name == "fetch" for name, _ in running), (
        "fetch server not in running set; check uvx + network"
    )
    client = fetch_manager.get_client("fetch")
    assert client is not None
    caps = client.server_capabilities
    assert "tools" in caps, f"fetch server should expose tools capability, got {caps}"


def test_fetch_call(fetch_manager: MCPManager) -> None:
    """Calling ``mcp__fetch__fetch`` on ``https://example.com`` must return a
    non-empty text content block."""
    handlers = build_mcp_handlers(fetch_manager)
    handler = handlers.get("mcp__fetch__fetch")
    assert handler is not None, f"mcp__fetch__fetch handler missing; got {list(handlers)}"
    out = handler(url="https://example.com")
    # Multimodal path returns list[dict]; error path returns string Error:.
    assert isinstance(out, list), f"expected list[dict], got {type(out).__name__}: {out!r}"
    assert out, "expected at least one content block"
    text_blocks = [b for b in out if isinstance(b, dict) and b.get("type") == "text"]
    assert text_blocks, f"expected at least one text block, got {out}"
    assert any(b.get("text") for b in text_blocks), "all text blocks were empty"
