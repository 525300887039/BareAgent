"""Real Python LSP end-to-end tests for the LSP integration.

The ``_manual.py`` suffix excludes this file from the default pytest run
(CI doesn't have a language server available). Run locally with::

    uv pip install -e ".[lsp]"   # pulls multilspy + jedi-language-server
    pytest tests/test_lsp_e2e_manual.py -v

multilspy 0.0.15 ships ``jedi-language-server`` as its Python adapter (see
``multilspy/language_server.py`` line ~76: ``Language.PYTHON → JediServer``).
Jedi is excellent for symbol navigation but does **not** report type errors
the way pyright does — diagnostics from jedi are mostly syntax / import
failures. The diagnostics test here checks that the cache machinery works
(jedi publishes ``[]`` for a clean file → handler reports "no diagnostics")
instead of trying to coerce a type error out of a non-type-checker.

Tests skip cleanly when ``jedi-language-server`` is unavailable so
contributors without the toolchain can still run the file.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from bareagent.lsp.config import LSPConfig, LSPServerConfig
from bareagent.lsp.manager import LanguageServerManager, ServerStatus
from bareagent.lsp.tools import build_lsp_tools

# Generous timeout — language server cold-starts + walks the project
# tree on first analysis.
LSP_TIMEOUT = 30.0


def _python_lsp_available() -> bool:
    # multilspy 0.0.15 → jedi-language-server. Check for it (and not pyright)
    # so the skip reason matches what the test actually exercises.
    return shutil.which("jedi-language-server") is not None


pytestmark = pytest.mark.skipif(
    not _python_lsp_available(),
    reason=('jedi-language-server not on PATH (install via `uv pip install -e ".[lsp]"`)'),
)


@pytest.fixture(scope="module")
def lsp_workspace(tmp_path_factory) -> Path:
    """A throwaway workspace with three modules covering the four E2E cases.

    * ``good.py`` — a typed function the outline test asserts on.
    * ``bad.py`` — has a syntax error so jedi-language-server actually
      reports a diagnostic (jedi doesn't do type-checking; the diagnostics
      test here verifies cache plumbing, not pyright-style type inference).
    * ``use.py`` — imports from ``good`` so the definition test can chase
      the symbol back to its source.
    """
    root = tmp_path_factory.mktemp("lsp_e2e")
    (root / "good.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (root / "bad.py").write_text(
        # Syntax error: jedi reports unterminated-string-literal / unexpected
        # token here, exercising the push-diagnostics path end-to-end.
        "x = 'unterminated\n",
        encoding="utf-8",
    )
    (root / "use.py").write_text(
        "from good import add\n\nresult = add(1, 2)\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def manager(lsp_workspace: Path):
    cfg = LSPConfig(
        servers=[
            LSPServerConfig(language="python", extensions=[".py", ".pyi"]),
        ],
        start_timeout=LSP_TIMEOUT,
    )
    mgr = LanguageServerManager(cfg, repository_root=str(lsp_workspace))
    mgr.start_all()
    yield mgr
    mgr.close_all()


# ---------------------------------------------------------------------------
# 1. Handshake — language server actually starts and reaches RUNNING
# ---------------------------------------------------------------------------


def test_pyright_handshake(manager: LanguageServerManager) -> None:
    from bareagent.lsp import MULTILSPY_AVAILABLE

    assert MULTILSPY_AVAILABLE is True
    assert manager.get_status("python") == ServerStatus.RUNNING
    running = dict(manager.iter_running())
    assert "python" in running


# ---------------------------------------------------------------------------
# 2. Outline — fetch documentSymbol for good.py
# ---------------------------------------------------------------------------


def test_pyright_outline(manager: LanguageServerManager, lsp_workspace: Path) -> None:
    _schemas, handlers = build_lsp_tools(manager)
    target = lsp_workspace / "good.py"
    output = handlers["lsp_outline"](file=str(target))
    # documentSymbol must surface our top-level function.
    assert "add" in output


# ---------------------------------------------------------------------------
# 3. Diagnostics — bad.py has a known syntax error
# ---------------------------------------------------------------------------


def test_pyright_diagnostics(manager: LanguageServerManager, lsp_workspace: Path) -> None:
    _schemas, handlers = build_lsp_tools(manager)
    target = lsp_workspace / "bad.py"

    # The diagnostics handler opens the file via multilspy's open_file
    # (which sends textDocument/didOpen), waits for publishDiagnostics via
    # the manager's per-file Event, then reads the cache. Some servers need
    # a few publishes before they settle — retry briefly.
    deadline = time.monotonic() + LSP_TIMEOUT
    output = ""
    while time.monotonic() < deadline:
        output = handlers["lsp_diagnostics"](file=str(target))
        if "no diagnostics" not in output:
            break
        time.sleep(0.5)
    assert "no diagnostics" not in output, (
        "language server never reported diagnostics for bad.py within timeout"
    )


# ---------------------------------------------------------------------------
# 4. Definition — use.py imports add from good.py
# ---------------------------------------------------------------------------


def test_pyright_definition(manager: LanguageServerManager, lsp_workspace: Path) -> None:
    _schemas, handlers = build_lsp_tools(manager)
    use_file = lsp_workspace / "use.py"
    # "result = add(1, 2)" — column points at 'add' (1-based col 10).
    output = ""
    deadline = time.monotonic() + LSP_TIMEOUT
    while time.monotonic() < deadline:
        output = handlers["lsp_definition"](file=str(use_file), line=3, col=10)
        if "no definition" not in output and "Error" not in output:
            break
        time.sleep(0.5)
    assert "no definition" not in output
    assert "good.py" in output


# ---------------------------------------------------------------------------
# 5. Semantic rename — rename ``add`` in good.py and verify the definition
#    file is rewritten (cross-file follow-up depends on the server; the
#    definition rewrite is the minimum jedi reliably produces).
# ---------------------------------------------------------------------------


def test_jedi_semantic_rename(manager: LanguageServerManager, lsp_workspace: Path) -> None:
    _schemas, handlers = build_lsp_tools(manager)
    good = lsp_workspace / "good.py"
    # ``def add(...)`` — 1-based col 5 points at the ``add`` identifier.
    output = ""
    deadline = time.monotonic() + LSP_TIMEOUT
    while time.monotonic() < deadline:
        output = handlers["semantic_rename"](file=str(good), line=1, col=5, new_name="plus")
        if not output.startswith("Error"):
            break
        time.sleep(0.5)
    assert not output.startswith("Error"), output
    # The definition site must have been renamed on disk.
    assert "def plus(" in good.read_text(encoding="utf-8")
    assert "edit" in output
