"""LSP server configuration parsing.

Reads a ``[lsp]`` block (plus ``[[lsp.servers]]`` array) from a TOML-derived
dict and returns typed dataclasses. Each server declares the multilspy
``code_language`` and the file extensions that route to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import LSPError

# 15s covers pyright startup + initial project scan on a medium repo. Larger
# projects (or rust-analyzer / jdtls) may need to bump this in user config.
_DEFAULT_START_TIMEOUT = 15.0


@dataclass(slots=True)
class LSPServerConfig:
    """One language server entry."""

    # multilspy ``code_language`` value (e.g. ``"python"`` / ``"typescript"`` /
    # ``"rust"``). Must be unique across the config — duplicates are rejected
    # by :func:`parse_lsp_config`.
    language: str
    # File suffixes (lowercased, leading dot) that route to this server.
    extensions: list[str] = field(default_factory=list)
    # Passed through to multilspy as ``initialization_options`` (LSP-specific
    # config; e.g. pyright's ``python.pythonPath``). ``None`` means "send no
    # options" — equivalent to ``null`` in LSP wire format.
    initialization_options: dict[str, Any] | None = None


@dataclass(slots=True)
class LSPConfig:
    """Top-level LSP configuration."""

    servers: list[LSPServerConfig] = field(default_factory=list)
    # Hybrid auto-diagnostics-on-edit feature flag. Parsed in this PR for
    # forward compatibility; the actual consumer lands in child B.
    auto_diagnostics_on_edit: bool = False
    # Per-server start timeout (seconds). Each server handshake gets this
    # budget independently inside ``LanguageServerManager.start_all``.
    start_timeout: float = _DEFAULT_START_TIMEOUT


def parse_lsp_config(raw: dict[str, Any]) -> LSPConfig:
    """Parse a TOML-derived dict (the ``[lsp]`` section) into ``LSPConfig``.

    Accepts either the full document (where ``lsp`` is a key) or the ``[lsp]``
    block itself. Missing or empty ``lsp`` yields an empty config with
    ``servers=[]``. Unknown keys are silently ignored to stay forward-compatible.
    """
    if not isinstance(raw, dict):
        raise LSPError(f"lsp config must be a table, got {type(raw).__name__}")

    block = raw.get("lsp", raw)
    if block is None:
        return LSPConfig()
    if not isinstance(block, dict):
        raise LSPError("'lsp' must be a table")

    cfg = LSPConfig(
        auto_diagnostics_on_edit=_bool(block, "auto_diagnostics_on_edit", False),
        start_timeout=_float(block, "start_timeout", _DEFAULT_START_TIMEOUT),
    )

    servers_raw = block.get("servers", [])
    if not isinstance(servers_raw, list):
        raise LSPError("'lsp.servers' must be an array of tables")

    seen: set[str] = set()
    for index, entry in enumerate(servers_raw):
        if not isinstance(entry, dict):
            raise LSPError(f"lsp.servers[{index}] must be a table")
        server = _parse_server(entry, index)
        if server.language in seen:
            raise LSPError(f"duplicate lsp server language: {server.language!r}")
        seen.add(server.language)
        cfg.servers.append(server)
    return cfg


def _parse_server(entry: dict[str, Any], index: int) -> LSPServerConfig:
    language = entry.get("language")
    if not isinstance(language, str) or not language:
        raise LSPError(
            f"lsp.servers[{index}].language is required and must be a non-empty string"
        )

    extensions_raw = entry.get("extensions")
    if not isinstance(extensions_raw, list) or not extensions_raw:
        raise LSPError(
            f"lsp.servers[{language}].extensions is required and must be a "
            "non-empty list of strings"
        )
    if not all(isinstance(ext, str) and ext for ext in extensions_raw):
        raise LSPError(
            f"lsp.servers[{language}].extensions must contain non-empty strings"
        )
    extensions = [ext.lower() for ext in extensions_raw]
    for ext in extensions:
        if not ext.startswith("."):
            raise LSPError(
                f"lsp.servers[{language}].extensions entries must start with '.', got {ext!r}"
            )

    init_options = entry.get("initialization_options")
    if init_options is not None and not isinstance(init_options, dict):
        raise LSPError(
            f"lsp.servers[{language}].initialization_options must be a table if provided"
        )

    return LSPServerConfig(
        language=language,
        extensions=extensions,
        initialization_options=dict(init_options) if init_options is not None else None,
    )


def _bool(block: dict[str, Any], key: str, default: bool) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise LSPError(f"lsp.{key} must be a boolean, got {value!r}")
    return value


def _float(block: dict[str, Any], key: str, default: float) -> float:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LSPError(f"lsp.{key} must be a number, got {value!r}")
    return float(value)
