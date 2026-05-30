"""Tests for ``src.core.tools`` LSP integration.

Verifies that the four ``lsp_*`` tool schemas land in ``get_tools()`` (even
without a live manager so the LLM always sees a stable surface) and that
``get_handlers(lsp_manager=mgr)`` binds real handlers.
"""

from __future__ import annotations

from src.core.tools import LSP_TOOL_SCHEMAS, get_handlers, get_tools
from src.lsp.config import LSPConfig, LSPServerConfig
from src.lsp.manager import LanguageServerManager
from src.lsp.tools import LSP_TOOL_NAMES


def test_lsp_schemas_present_in_deferred_tools() -> None:
    """LSP tool schemas live in ``DEFERRED_TOOL_SCHEMAS`` so they are
    visible even when no manager is wired."""
    names = {s["name"] for s in get_tools()}
    for tool in LSP_TOOL_NAMES:
        assert tool in names


def test_lsp_handlers_fallback_when_no_manager(tmp_path) -> None:
    handlers = get_handlers(workspace=tmp_path)
    for tool in LSP_TOOL_NAMES:
        assert tool in handlers
        # Fallback handlers must accept arbitrary kwargs and return a string.
        result = handlers[tool](file="x.py", line=1, col=1)
        assert isinstance(result, str)
        assert result.startswith("Error:")


def test_get_handlers_binds_real_lsp_handlers(monkeypatch, tmp_path) -> None:
    # Build a manager pointed at an empty config so start_all() is a no-op,
    # then inject a fake server entry directly so the handler resolves.
    cfg = LSPConfig(servers=[LSPServerConfig(language="python", extensions=[".py"])])
    mgr = LanguageServerManager(cfg, repository_root=str(tmp_path))

    handlers = get_handlers(workspace=tmp_path, lsp_manager=mgr)
    # When the manager has no running server, the handler still resolves and
    # returns an "unhealthy" error string.
    sample = tmp_path / "foo.py"
    sample.write_text("x = 1")
    output = handlers["lsp_outline"](file=str(sample))
    assert isinstance(output, str)
    assert output.startswith("Error:")
    assert "unhealthy" in output


def test_lsp_tool_schemas_module_constant_matches() -> None:
    names = {s["name"] for s in LSP_TOOL_SCHEMAS}
    assert names == set(LSP_TOOL_NAMES)
