"""LSP (Language Server Protocol) client subpackage.

PR1 (this child) delivers the multi-language manager, four Tier-1 tools
(``lsp_outline`` / ``lsp_definition`` / ``lsp_references`` /
``lsp_diagnostics``), config parsing, and agent-type integration. UX (REPL
commands), atexit cleanup, hybrid auto-diagnostics, and the real
``[lsp]`` extra E2E test land in the sibling child task.

``multilspy`` is an **optional dependency**. Importing this package never
raises when the extra is missing — the boolean ``MULTILSPY_AVAILABLE`` lets
callers feature-detect, and :class:`LanguageServerManager` degrades to a
no-op when the underlying library is unavailable.
"""

from __future__ import annotations

from .config import LSPConfig, LSPServerConfig, parse_lsp_config
from .errors import LSPCallError, LSPError, LSPHandshakeError
from .manager import LanguageServerManager, ServerStatus
from .tools import LSP_TOOL_NAMES, LSP_TOOL_SCHEMAS, build_lsp_tools


def _detect_multilspy() -> bool:
    """Return True iff ``import multilspy`` succeeds at package-import time.

    The check is done eagerly here so callers can branch on the boolean
    without paying an import every call. Errors other than ``ImportError``
    (rare, e.g. a broken install) are treated as "not available".
    """
    try:
        import multilspy  # noqa: F401  # type: ignore
    except Exception:
        return False
    return True


MULTILSPY_AVAILABLE: bool = _detect_multilspy()


__all__ = [
    "LSPCallError",
    "LSPConfig",
    "LSPError",
    "LSPHandshakeError",
    "LSPServerConfig",
    "LSP_TOOL_NAMES",
    "LSP_TOOL_SCHEMAS",
    "LanguageServerManager",
    "MULTILSPY_AVAILABLE",
    "ServerStatus",
    "build_lsp_tools",
    "parse_lsp_config",
]
