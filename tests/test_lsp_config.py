"""Tests for ``src.lsp.config`` — TOML-derived LSP configuration parsing."""

from __future__ import annotations

import pytest

from bareagent.lsp.config import LSPConfig, parse_lsp_config
from bareagent.lsp.errors import LSPError


def _wrap(servers: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
    return {"lsp": {"servers": servers, **kwargs}}


def test_parses_python_typescript_rust() -> None:
    raw = _wrap(
        [
            {"language": "python", "extensions": [".py", ".pyi"]},
            {
                "language": "typescript",
                "extensions": [".ts", ".tsx", ".JS"],  # tests case normalization
            },
            {
                "language": "rust",
                "extensions": [".rs"],
                "initialization_options": {"rust-analyzer": {"check": "off"}},
            },
        ],
        auto_diagnostics_on_edit=True,
        start_timeout=30.0,
    )
    cfg = parse_lsp_config(raw)
    assert isinstance(cfg, LSPConfig)
    assert cfg.auto_diagnostics_on_edit is True
    assert cfg.start_timeout == 30.0
    assert [s.language for s in cfg.servers] == ["python", "typescript", "rust"]
    # Extensions are normalized to lowercase.
    assert cfg.servers[1].extensions == [".ts", ".tsx", ".js"]
    assert cfg.servers[2].initialization_options == {"rust-analyzer": {"check": "off"}}


def test_empty_config_returns_empty_servers() -> None:
    cfg = parse_lsp_config({})
    assert cfg.servers == []
    assert cfg.auto_diagnostics_on_edit is False
    assert cfg.start_timeout == 15.0


def test_missing_lsp_block_returns_empty_config() -> None:
    cfg = parse_lsp_config({"other": "stuff"})
    # When the dict has no ``lsp`` key the parser treats it as a config block
    # itself; with no ``servers`` key the result is an empty config.
    assert cfg.servers == []


def test_duplicate_language_raises() -> None:
    with pytest.raises(LSPError, match="duplicate lsp server language"):
        parse_lsp_config(
            _wrap(
                [
                    {"language": "python", "extensions": [".py"]},
                    {"language": "python", "extensions": [".pyi"]},
                ]
            )
        )


def test_missing_language_raises() -> None:
    with pytest.raises(LSPError, match="language is required"):
        parse_lsp_config(_wrap([{"extensions": [".py"]}]))


def test_missing_extensions_raises() -> None:
    with pytest.raises(LSPError, match="extensions is required"):
        parse_lsp_config(_wrap([{"language": "python"}]))


def test_empty_extensions_raises() -> None:
    with pytest.raises(LSPError, match="extensions is required"):
        parse_lsp_config(_wrap([{"language": "python", "extensions": []}]))


def test_extensions_must_start_with_dot() -> None:
    with pytest.raises(LSPError, match="must start with '.'"):
        parse_lsp_config(_wrap([{"language": "python", "extensions": ["py"]}]))


def test_initialization_options_must_be_dict() -> None:
    with pytest.raises(LSPError, match="initialization_options"):
        parse_lsp_config(
            _wrap(
                [
                    {
                        "language": "python",
                        "extensions": [".py"],
                        "initialization_options": "not-a-dict",
                    }
                ]
            )
        )
