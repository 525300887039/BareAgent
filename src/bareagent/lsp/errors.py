"""LSP error hierarchy.

Layered failure types: handshake (initialize lifecycle) and call (a request to
the language server raised or timed out). LSP tool execution failures
(``request_*`` returning empty or a server-side error) are NOT exceptions —
they flow back to the LLM as text via the tool handlers (see ``tools.py``).
"""

from __future__ import annotations


class LSPError(Exception):
    """Base class for all LSP-related failures."""


class LSPHandshakeError(LSPError):
    """Language server initialize/start handshake failed: timeout, multilspy
    refused to launch, or the underlying server crashed during startup."""


class LSPCallError(LSPError):
    """A ``request_*`` call (definition / references / outline / diagnostics)
    raised inside multilspy. Handlers catch this and return ``str(exc)`` to the
    LLM."""
