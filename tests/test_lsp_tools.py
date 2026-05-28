"""Tests for ``src.lsp.tools`` — the four Tier-1 LSP tool handlers.

Uses a hand-rolled stub :class:`FakeServer` injected into the manager so
multilspy never needs to spawn a real language server.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.lsp.manager import ServerStatus
from src.lsp.tools import LSP_TOOL_NAMES, LSP_TOOL_SCHEMAS, build_lsp_tools


class FakeServer:
    """Minimal stub of multilspy's ``SyncLanguageServer`` request surface."""

    def __init__(self) -> None:
        self.document_symbols_response: Any = ([], None)
        self.definition_response: list[dict[str, Any]] = []
        self.references_response: list[dict[str, Any]] = []
        self.diagnostics_pull: list[dict[str, Any]] | None = None
        self.diagnostics_pull_raises: Exception | None = None
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.raise_on: dict[str, Exception] = {}
        self.last_definition_args: tuple[str, int, int] | None = None
        self.last_references_args: tuple[str, int, int] | None = None
        self.last_outline_arg: str | None = None

    def request_document_symbols(self, relative_file_path: str):
        if "outline" in self.raise_on:
            raise self.raise_on["outline"]
        self.last_outline_arg = relative_file_path
        return self.document_symbols_response

    def request_definition(self, relative_file_path: str, line: int, column: int):
        if "definition" in self.raise_on:
            raise self.raise_on["definition"]
        self.last_definition_args = (relative_file_path, line, column)
        return self.definition_response

    def request_references(self, relative_file_path: str, line: int, column: int):
        if "references" in self.raise_on:
            raise self.raise_on["references"]
        self.last_references_args = (relative_file_path, line, column)
        return self.references_response

    def request_text_document_diagnostics(self, relative_file_path: str):
        if self.diagnostics_pull_raises is not None:
            raise self.diagnostics_pull_raises
        if self.diagnostics_pull is None:
            raise NotImplementedError
        return self.diagnostics_pull


class _StubManager:
    """Minimal LanguageServerManager-compatible stub used by build_lsp_tools.

    The real manager spawns multilspy on ``start_all``. Tests need control over
    which server gets returned for which file, plus the ability to simulate
    UNHEALTHY routing — that's all this stub exposes.
    """

    def __init__(
        self,
        repository_root: str,
        *,
        servers: dict[str, FakeServer] | None = None,
        statuses: dict[str, ServerStatus] | None = None,
        ext_map: dict[str, str] | None = None,
    ) -> None:
        self.repository_root = repository_root
        self._servers = servers or {}
        self._statuses = statuses or {}
        self._ext_map = ext_map or {}

    def language_for_file(self, path: str) -> str | None:
        _, ext = os.path.splitext(path)
        return self._ext_map.get(ext.lower())

    def get_server_for_file(self, path: str):
        language = self.language_for_file(path)
        if language is None:
            return None
        if self._statuses.get(language) != ServerStatus.RUNNING:
            return None
        return self._servers.get(language)


@pytest.fixture
def fake_setup(tmp_path):
    """Build a stub manager + handler bundle pointing at a real on-disk file
    so the file-not-found error path works correctly in tests."""
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n")
    fake_server = FakeServer()
    stub = _StubManager(
        repository_root=str(tmp_path),
        servers={"python": fake_server},
        statuses={"python": ServerStatus.RUNNING},
        ext_map={".py": "python"},
    )
    schemas, handlers = build_lsp_tools(stub)  # type: ignore[arg-type]
    return {
        "tmp_path": tmp_path,
        "sample": sample,
        "server": fake_server,
        "manager": stub,
        "schemas": schemas,
        "handlers": handlers,
    }


# ---------------------------------------------------------------------------
# Schema / registry surface
# ---------------------------------------------------------------------------


def test_build_lsp_tools_returns_four_schemas_and_handlers(fake_setup) -> None:
    schemas = fake_setup["schemas"]
    handlers = fake_setup["handlers"]
    names = {s["name"] for s in schemas}
    assert names == set(LSP_TOOL_NAMES)
    assert set(handlers) == set(LSP_TOOL_NAMES)
    # Every schema must mention the 1-based coordinate convention so the LLM
    # never sends 0-based positions.
    coord_text = "1-based"
    for schema in schemas:
        if schema["name"] in {"lsp_definition", "lsp_references"}:
            assert coord_text in schema["description"]


def test_lsp_tool_schemas_module_constant_matches() -> None:
    names = {s["name"] for s in LSP_TOOL_SCHEMAS}
    assert names == set(LSP_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------


def test_outline_returns_indented_tree(fake_setup) -> None:
    fake_setup["server"].document_symbols_response = (
        [
            {
                "name": "Foo",
                "kind": 5,  # Class
                "location": {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 9, "character": 0},
                    }
                },
            },
            {
                "name": "bar",
                "kind": 12,  # Function
                "location": {
                    "range": {
                        "start": {"line": 11, "character": 0},
                        "end": {"line": 13, "character": 0},
                    }
                },
            },
        ],
        None,
    )
    output = fake_setup["handlers"]["lsp_outline"](file=str(fake_setup["sample"]))
    assert "class Foo" in output
    assert "function bar" in output
    # 1-based line numbers in human output.
    assert "lines 1-10" in output
    assert "lines 12-14" in output


def test_outline_empty_returns_placeholder(fake_setup) -> None:
    fake_setup["server"].document_symbols_response = ([], None)
    output = fake_setup["handlers"]["lsp_outline"](file=str(fake_setup["sample"]))
    assert output == "(no symbols)"


# ---------------------------------------------------------------------------
# Definition / coordinate conversion
# ---------------------------------------------------------------------------


def test_definition_converts_coordinates_to_0_based(fake_setup) -> None:
    fake_setup["server"].definition_response = [
        {
            "absolutePath": str(fake_setup["sample"]),
            "uri": "file:///tmp/sample.py",
            "range": {
                "start": {"line": 4, "character": 2},
                "end": {"line": 4, "character": 5},
            },
        }
    ]
    output = fake_setup["handlers"]["lsp_definition"](
        file=str(fake_setup["sample"]), line=10, col=5
    )
    # 1-based (10, 5) → 0-based (9, 4) sent to multilspy.
    relpath, line0, col0 = fake_setup["server"].last_definition_args
    assert (line0, col0) == (9, 4)
    # Output uses 1-based coordinates (server returned line 4, char 2 → 5:3).
    assert "5:3" in output
    # Path is rendered relative to the repo root.
    assert relpath == "sample.py" or relpath.endswith("sample.py")


def test_definition_no_results(fake_setup) -> None:
    fake_setup["server"].definition_response = []
    output = fake_setup["handlers"]["lsp_definition"](
        file=str(fake_setup["sample"]), line=1, col=1
    )
    assert "no definition" in output


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_references_returns_each_location(fake_setup) -> None:
    fake_setup["server"].references_response = [
        {
            "absolutePath": str(fake_setup["sample"]),
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
        },
        {
            "absolutePath": str(fake_setup["sample"]),
            "range": {
                "start": {"line": 4, "character": 0},
                "end": {"line": 4, "character": 1},
            },
        },
    ]
    output = fake_setup["handlers"]["lsp_references"](
        file=str(fake_setup["sample"]), line=10, col=5
    )
    # Coordinates converted before the call.
    _relpath, line0, col0 = fake_setup["server"].last_references_args
    assert (line0, col0) == (9, 4)
    # Two reference rows, both 1-based.
    assert "1:1" in output
    assert "5:1" in output


# ---------------------------------------------------------------------------
# Diagnostics: pull + push fallback
# ---------------------------------------------------------------------------


def test_diagnostics_pull_path(fake_setup) -> None:
    fake_setup["server"].diagnostics_pull = [
        {
            "severity": 1,
            "message": "name 'x' is not defined",
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
        }
    ]
    output = fake_setup["handlers"]["lsp_diagnostics"](file=str(fake_setup["sample"]))
    assert "[Error] Line 1" in output
    assert "name 'x' is not defined" in output


def test_diagnostics_falls_back_to_push_cache(fake_setup) -> None:
    # Pull raises (e.g. NotImplementedError) → handler reads push cache.
    fake_setup["server"].diagnostics_pull_raises = NotImplementedError()
    # Manager stores ``diagnostics`` indexed by the same relative path the
    # handler uses (``os.path.relpath(abs_path, repo_root)``).
    rel = os.path.relpath(
        str(fake_setup["sample"]),
        start=fake_setup["manager"].repository_root,
    )
    fake_setup["server"].diagnostics = {
        rel: [
            {
                "severity": 2,
                "message": "deprecated import",
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 1, "character": 1},
                },
            }
        ]
    }
    output = fake_setup["handlers"]["lsp_diagnostics"](file=str(fake_setup["sample"]))
    assert "[Warning] Line 2" in output
    assert "deprecated import" in output


def test_diagnostics_no_results(fake_setup) -> None:
    fake_setup["server"].diagnostics_pull = []
    output = fake_setup["handlers"]["lsp_diagnostics"](file=str(fake_setup["sample"]))
    assert "no diagnostics" in output


# ---------------------------------------------------------------------------
# Error degradation paths
# ---------------------------------------------------------------------------


def test_no_route_returns_error(fake_setup, tmp_path) -> None:
    other = tmp_path / "foo.rs"
    other.write_text("fn main() {}")
    output = fake_setup["handlers"]["lsp_outline"](file=str(other))
    assert output.startswith("Error: no LSP server configured for .rs")


def test_unhealthy_server_returns_error(fake_setup) -> None:
    # Flip the stub status so routing fails the "must be RUNNING" check.
    fake_setup["manager"]._statuses["python"] = ServerStatus.UNHEALTHY
    output = fake_setup["handlers"]["lsp_outline"](file=str(fake_setup["sample"]))
    assert output == "Error: language server 'python' is unhealthy"


def test_missing_file_returns_error(fake_setup) -> None:
    output = fake_setup["handlers"]["lsp_outline"](file="does/not/exist.py")
    assert output.startswith("Error: file not found")


def test_handler_catches_unexpected_exception(fake_setup) -> None:
    fake_setup["server"].raise_on["outline"] = RuntimeError("boom")
    output = fake_setup["handlers"]["lsp_outline"](file=str(fake_setup["sample"]))
    assert output.startswith("Error: LSP call failed: RuntimeError: boom")


def test_handler_returns_error_string_never_raises(fake_setup) -> None:
    # All four handlers must return a string (success or failure) — never raise.
    for tool in LSP_TOOL_NAMES:
        if tool in {"lsp_definition", "lsp_references"}:
            out = fake_setup["handlers"][tool](file="x.unknown", line=1, col=1)
        else:
            out = fake_setup["handlers"][tool](file="x.unknown")
        assert isinstance(out, str)
        assert out.startswith("Error:")
