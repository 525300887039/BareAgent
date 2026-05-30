"""Tests for ``src.lsp.diagnostics`` — diff algorithm + Hybrid hook helpers."""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.lsp.config import LSPConfig
from src.lsp.diagnostics import (
    Diagnostic,
    DiagnosticKey,
    diff_diagnostics,
    format_diagnostics,
    maybe_diagnostics_appendix,
    snapshot_diagnostics,
)

# ---------------------------------------------------------------------------
# DiagnosticKey / diff algorithm
# ---------------------------------------------------------------------------


def _d(
    file: str = "src/foo.py",
    line: int = 12,
    col: int = 5,
    severity: str = "Error",
    message: str = "boom",
    source: str = "pyright",
) -> Diagnostic:
    return Diagnostic(
        file=file, line=line, col=col, severity=severity, message=message, source=source
    )


def test_diff_diagnostics_identifies_new_row_by_five_tuple() -> None:
    before = [_d(line=10, message="old"), _d(line=11, message="other")]
    after = [
        _d(line=10, message="old"),  # unchanged → filtered out
        _d(line=12, message="new"),  # new row → kept
    ]
    new = diff_diagnostics(before, after)
    assert len(new) == 1
    assert new[0].line == 12 and new[0].message == "new"


def test_diff_diagnostics_message_change_counts_as_new() -> None:
    before = [_d(line=12, message="A")]
    after = [_d(line=12, message="B")]  # same position, different text
    new = diff_diagnostics(before, after)
    assert len(new) == 1 and new[0].message == "B"


def test_diff_diagnostics_source_difference_is_ignored() -> None:
    # The 5-tuple deliberately excludes ``source`` — pyright sometimes
    # leaves it blank, which would otherwise cause spurious "new" hits.
    before = [_d(source="pyright")]
    after = [_d(source="")]
    assert diff_diagnostics(before, after) == []


def test_diff_diagnostics_preserves_after_order() -> None:
    before: list[Diagnostic] = []
    after = [
        _d(line=20, message="bottom"),
        _d(line=5, message="top"),
        _d(line=12, message="mid"),
    ]
    new = diff_diagnostics(before, after)
    assert [d.line for d in new] == [20, 5, 12]


def test_diff_diagnostics_empty_inputs_return_empty() -> None:
    assert diff_diagnostics([], []) == []
    assert diff_diagnostics([_d()], [_d()]) == []


def test_diagnostic_key_equality() -> None:
    a = DiagnosticKey.from_diag(_d())
    b = DiagnosticKey.from_diag(_d())  # identical 5-tuple
    c = DiagnosticKey.from_diag(_d(message="different"))
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# format_diagnostics
# ---------------------------------------------------------------------------


def test_format_diagnostics_prd_shape() -> None:
    rendered = format_diagnostics(
        "src/foo.py",
        [
            _d(
                line=12,
                col=5,
                severity="Error",
                message="Cannot assign to variable 'x' because of its type",
                source="pyright",
            )
        ],
    )
    # Header is exact text from the PRD so downstream tooling can detect it.
    assert rendered.startswith("Newly introduced diagnostics in src/foo.py:")
    assert (
        "- [pyright Error] Line 12:5 — Cannot assign to variable 'x' because of its type"
        in rendered
    )


def test_format_diagnostics_blank_source_falls_back_to_lsp() -> None:
    rendered = format_diagnostics("a.py", [_d(source="")])
    assert "[lsp Error]" in rendered


def test_format_diagnostics_empty_returns_header_only() -> None:
    # Implementation detail — callers normally short-circuit before this
    # path. Still asserting so the contract is documented.
    assert format_diagnostics("src/foo.py", []) == (
        "Newly introduced diagnostics in src/foo.py:"
    )


# ---------------------------------------------------------------------------
# snapshot_diagnostics — uses a stub manager because we don't run multilspy.
# ---------------------------------------------------------------------------


class _StubManager:
    """Just enough of LanguageServerManager for ``snapshot_diagnostics``."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        language: str | None = "python",
        repository_root: str = ".",
    ) -> None:
        self._rows = rows
        self._language = language
        self.repository_root = repository_root
        self.config = LSPConfig(auto_diagnostics_on_edit=True)
        self._wait_called_with: tuple[str, float] | None = None

    def language_for_file(self, path: str) -> str | None:
        return self._language

    def get_server_for_file(self, path: str) -> Any:
        return object() if self._language else None

    def get_diagnostics_snapshot(self, file_path: str) -> list[dict[str, Any]]:
        return list(self._rows)

    def wait_for_diagnostics(self, file_path: str, timeout: float = 1.5) -> bool:
        self._wait_called_with = (file_path, timeout)
        return True


def test_snapshot_diagnostics_normalizes_rows() -> None:
    mgr = _StubManager(
        [
            {
                "severity": 1,
                "message": "boom",
                "source": "pyright",
                "range": {
                    "start": {"line": 11, "character": 4},  # 0-based 11/4
                    "end": {"line": 11, "character": 5},
                },
            }
        ]
    )
    diags = snapshot_diagnostics(mgr, "src/foo.py")  # type: ignore[arg-type]
    assert len(diags) == 1
    assert diags[0].line == 12 and diags[0].col == 5
    assert diags[0].severity == "Error" and diags[0].source == "pyright"


def test_snapshot_diagnostics_skips_malformed_rows() -> None:
    mgr = _StubManager(
        [
            "not a dict",  # type: ignore[list-item]
            {"severity": 2, "message": "no range"},
            {
                "severity": 2,
                "message": "good",
                "range": {"start": {"line": 0, "character": 0}},
            },
        ]
    )
    diags = snapshot_diagnostics(mgr, "src/foo.py")  # type: ignore[arg-type]
    assert len(diags) == 1
    assert diags[0].message == "good"


# ---------------------------------------------------------------------------
# maybe_diagnostics_appendix — the public hook contract
# ---------------------------------------------------------------------------


def test_appendix_returns_none_when_manager_missing() -> None:
    assert maybe_diagnostics_appendix(None, LSPConfig(), "a.py", []) is None


def test_appendix_returns_none_when_config_off() -> None:
    mgr = _StubManager([])
    cfg = LSPConfig(auto_diagnostics_on_edit=False)
    assert maybe_diagnostics_appendix(mgr, cfg, "a.py", []) is None  # type: ignore[arg-type]


def test_appendix_returns_none_when_no_route() -> None:
    mgr = _StubManager([], language=None)
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    assert maybe_diagnostics_appendix(mgr, cfg, "a.unknown", []) is None  # type: ignore[arg-type]


def test_appendix_happy_path_appends_when_new_diag_appears() -> None:
    new_row = {
        "severity": 1,
        "message": "Cannot assign",
        "source": "pyright",
        "range": {
            "start": {"line": 11, "character": 4},
            "end": {"line": 11, "character": 5},
        },
    }
    mgr = _StubManager([new_row])
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    appendix = maybe_diagnostics_appendix(mgr, cfg, "src/foo.py", [])  # type: ignore[arg-type]
    assert appendix is not None
    assert appendix.startswith("\n\n")
    assert "Newly introduced diagnostics in src/foo.py:" in appendix
    assert "[pyright Error] Line 12:5 — Cannot assign" in appendix


def test_appendix_returns_none_when_diagnostic_unchanged() -> None:
    existing_row = {
        "severity": 1,
        "message": "boom",
        "source": "pyright",
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    mgr = _StubManager([existing_row])
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    before = [_d(line=1, col=1, severity="Error", message="boom", source="pyright")]
    # Same 5-tuple before vs after → no new rows → no appendix.
    assert maybe_diagnostics_appendix(mgr, cfg, "src/foo.py", before) is None  # type: ignore[arg-type]


def test_appendix_calls_wait_for_diagnostics() -> None:
    # Pyright analysis lag mitigation — the hook must give the server a
    # moment to publish after a write.
    mgr = _StubManager([])
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    maybe_diagnostics_appendix(mgr, cfg, "src/foo.py", [])  # type: ignore[arg-type]
    assert mgr._wait_called_with is not None
    file_arg, timeout_arg = mgr._wait_called_with
    assert file_arg == "src/foo.py"
    assert 0.1 < timeout_arg <= 5.0


def test_appendix_swallows_exceptions_from_manager() -> None:
    class _ExplodingManager(_StubManager):
        def wait_for_diagnostics(self, file_path: str, timeout: float = 1.5) -> bool:
            raise RuntimeError("simulated multilspy crash")

    mgr = _ExplodingManager([])
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    # The hook must never raise — handlers depend on that contract.
    assert maybe_diagnostics_appendix(mgr, cfg, "src/foo.py", []) is None  # type: ignore[arg-type]


# Stay quiet about an unused import in the no-multilspy path.
_ = pytest
_ = os
