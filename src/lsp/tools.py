"""LSP -> BareAgent tool schema + handler factory.

Exposes four Tier-1 LSP capabilities to the LLM under the ``lsp_*`` prefix:

* ``lsp_outline(file)`` — ``textDocument/documentSymbol``
* ``lsp_definition(file, line, col)`` — ``textDocument/definition``
* ``lsp_references(file, line, col)`` — ``textDocument/references``
* ``lsp_diagnostics(file)`` — published-diagnostics cache (pull request API
  is not yet surfaced by multilspy, so we read whatever the underlying
  language-server handler has buffered).

The schema marks ``line`` / ``col`` as **1-based** to match editor convention.
Handlers convert to LSP's 0-based form internally before calling multilspy.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.core.schema import tool_schema as _schema

from .coord import line_col_0_to_1, line_col_1_to_0, to_repo_relative

if TYPE_CHECKING:
    from .manager import LanguageServerManager

LSP_TOOL_NAMES = (
    "lsp_outline",
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
)

_COORD_DOC = (
    "line and column are 1-based (matching editor convention). "
    "Position (1, 1) is the very first character of the file."
)


LSP_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "lsp_outline",
        (
            "Return a hierarchical symbol outline (classes, functions, "
            "methods, variables) for a single file using the language server's "
            "documentSymbol response. Cheaper than reading the whole file when "
            "you want to understand its shape."
        ),
        {
            "file": {
                "type": "string",
                "description": "Workspace-relative or absolute path to the file.",
            },
        },
        ["file"],
    ),
    _schema(
        "lsp_definition",
        ("Jump to the definition of the symbol at the given position. " + _COORD_DOC),
        {
            "file": {
                "type": "string",
                "description": "Workspace-relative or absolute path to the file.",
            },
            "line": {
                "type": "integer",
                "description": "1-based line number of the symbol.",
                "minimum": 1,
            },
            "col": {
                "type": "integer",
                "description": "1-based column number of the symbol.",
                "minimum": 1,
            },
        },
        ["file", "line", "col"],
    ),
    _schema(
        "lsp_references",
        ("List every reference to the symbol at the given position. " + _COORD_DOC),
        {
            "file": {
                "type": "string",
                "description": "Workspace-relative or absolute path to the file.",
            },
            "line": {
                "type": "integer",
                "description": "1-based line number of the symbol.",
                "minimum": 1,
            },
            "col": {
                "type": "integer",
                "description": "1-based column number of the symbol.",
                "minimum": 1,
            },
        },
        ["file", "line", "col"],
    ),
    _schema(
        "lsp_diagnostics",
        (
            "Return the language server's diagnostics for a single file "
            "(errors, warnings, hints). Prefers the pull-diagnostics request "
            "when available; otherwise falls back to the publishDiagnostics "
            "cache."
        ),
        {
            "file": {
                "type": "string",
                "description": "Workspace-relative or absolute path to the file.",
            },
        },
        ["file"],
    ),
]


def build_lsp_tools(
    manager: LanguageServerManager,
) -> tuple[list[dict[str, Any]], dict[str, Callable[..., Any]]]:
    """Return ``(schemas, handlers)`` for the four Tier-1 LSP tools.

    Schemas are stable across managers; only the handlers close over
    ``manager`` so they can look up the live server on every call.
    """
    schemas = [dict(schema) for schema in LSP_TOOL_SCHEMAS]
    handlers: dict[str, Callable[..., Any]] = {
        "lsp_outline": _make_outline_handler(manager),
        "lsp_definition": _make_definition_handler(manager),
        "lsp_references": _make_references_handler(manager),
        "lsp_diagnostics": _make_diagnostics_handler(manager),
    }
    return schemas, handlers


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_outline_handler(
    manager: LanguageServerManager,
) -> Callable[..., str]:
    def _handler(file: str) -> str:
        prelude = _prelude_or_error(manager, file)
        if isinstance(prelude, str):
            return prelude
        server, relpath = prelude
        try:
            result = server.request_document_symbols(relpath)
        except Exception as exc:
            return f"Error: LSP call failed: {type(exc).__name__}: {exc}"
        return _format_outline(result)

    return _handler


def _make_definition_handler(
    manager: LanguageServerManager,
) -> Callable[..., str]:
    def _handler(file: str, line: int, col: int) -> str:
        prelude = _prelude_or_error(manager, file)
        if isinstance(prelude, str):
            return prelude
        server, relpath = prelude
        line0, col0 = line_col_1_to_0(line, col)
        try:
            locations = server.request_definition(relpath, line0, col0)
        except Exception as exc:
            return f"Error: LSP call failed: {type(exc).__name__}: {exc}"
        if not locations:
            return "(no definition found)"
        return _format_locations(locations, manager)

    return _handler


def _make_references_handler(
    manager: LanguageServerManager,
) -> Callable[..., str]:
    def _handler(file: str, line: int, col: int) -> str:
        prelude = _prelude_or_error(manager, file)
        if isinstance(prelude, str):
            return prelude
        server, relpath = prelude
        line0, col0 = line_col_1_to_0(line, col)
        try:
            locations = server.request_references(relpath, line0, col0)
        except Exception as exc:
            return f"Error: LSP call failed: {type(exc).__name__}: {exc}"
        if not locations:
            return "(no references found)"
        return _format_locations(locations, manager)

    return _handler


def _make_diagnostics_handler(
    manager: LanguageServerManager,
) -> Callable[..., str]:
    def _handler(file: str) -> str:
        prelude = _prelude_or_error(manager, file)
        if isinstance(prelude, str):
            return prelude
        server, relpath = prelude

        # Try pull-diagnostics first (LSP 3.17+). multilspy >= 0.0.15 does
        # not expose this on SyncLanguageServer; fall through to the push
        # cache when the method is missing.
        diagnostics: list[Any] | None = None
        pull = getattr(server, "request_text_document_diagnostics", None)
        if callable(pull):
            try:
                diagnostics = list(pull(relpath))
            except Exception as exc:
                # Pull failed; we still try the push cache below before
                # giving up.
                pull_error = f"{type(exc).__name__}: {exc}"
                diagnostics = None
                _ = pull_error

        if not diagnostics:
            diagnostics = _read_push_diagnostics(server, relpath)

        if not diagnostics:
            return "(no diagnostics)"
        return _format_diagnostics(diagnostics)

    return _handler


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _prelude_or_error(
    manager: LanguageServerManager,
    file: str,
) -> tuple[Any, str] | str:
    """Validate input and return ``(server, relative_path)`` or an error string.

    Centralizes the file-not-found / no-route / unhealthy-server checks so
    every handler returns the same error wording for the same failure mode.
    """
    if not file:
        return "Error: file argument is required"

    # Resolve absolute path so existence + routing both work whether the
    # caller supplied a workspace-relative or absolute path.
    abs_path = file if os.path.isabs(file) else os.path.abspath(file)
    if not os.path.exists(abs_path):
        return f"Error: file not found: {file}"

    language = manager.language_for_file(abs_path)
    if language is None:
        _, ext = os.path.splitext(abs_path)
        ext_display = ext or "(no extension)"
        return f"Error: no LSP server configured for {ext_display}"

    server = manager.get_server_for_file(abs_path)
    if server is None:
        return f"Error: language server {language!r} is unhealthy"

    relpath = to_repo_relative(abs_path, manager.repository_root)
    return server, relpath


def _format_outline(result: Any) -> str:
    """Render multilspy's ``request_document_symbols`` return value as a
    plain text indented tree."""
    symbols: list[dict[str, Any]] = []
    tree: Any = None
    if isinstance(result, tuple) and len(result) >= 1:
        symbols = list(result[0]) if result[0] else []
        if len(result) >= 2:
            tree = result[1]
    elif isinstance(result, list):
        symbols = list(result)

    if tree:
        rendered = _render_tree(tree, symbols)
        if rendered:
            return rendered

    if not symbols:
        return "(no symbols)"
    return "\n".join(_format_symbol_flat(sym) for sym in symbols)


def _render_tree(
    tree: Any,
    symbols: list[dict[str, Any]],
    *,
    depth: int = 0,
) -> str:
    """Best-effort rendering of multilspy's ``TreeRepr`` (``Dict[int, List]``).

    The TreeRepr maps a symbol index to its child indices. Falls back to a
    flat listing when the tree is malformed.
    """
    if not isinstance(tree, dict):
        return ""
    if not symbols:
        return ""

    lines: list[str] = []
    visited: set[int] = set()

    def _walk(node: Any, level: int) -> None:
        if not isinstance(node, dict):
            return
        for raw_index, children in node.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if index in visited or not (0 <= index < len(symbols)):
                continue
            visited.add(index)
            lines.append("  " * level + _format_symbol_flat(symbols[index]))
            if isinstance(children, list):
                for child in children:
                    _walk(child, level + 1)
                    if isinstance(child, dict):
                        _walk(child, level + 1)

    _walk(tree, depth)
    return "\n".join(lines)


def _format_symbol_flat(sym: dict[str, Any]) -> str:
    name = sym.get("name", "?")
    kind = _symbol_kind_label(sym.get("kind"))
    location = sym.get("location") or {}
    range_ = location.get("range") if isinstance(location, dict) else None
    if isinstance(range_, dict):
        start = range_.get("start") or {}
        end = range_.get("end") or {}
        start_line, _ = line_col_0_to_1(
            int(start.get("line", 0) or 0),
            int(start.get("character", 0) or 0),
        )
        end_line, _ = line_col_0_to_1(
            int(end.get("line", 0) or 0),
            int(end.get("character", 0) or 0),
        )
        if start_line == end_line:
            range_part = f"line {start_line}"
        else:
            range_part = f"lines {start_line}-{end_line}"
    else:
        range_part = "line ?"
    return f"{kind} {name} ({range_part})"


def _symbol_kind_label(kind: Any) -> str:
    """Map an LSP ``SymbolKind`` numeric value to a short label."""
    # Subset of LSP SymbolKind that the outline cares about. Anything else
    # falls through to a generic "symbol" label so the renderer never crashes
    # on a server that returns a numeric kind we don't know yet.
    labels = {
        2: "module",
        3: "namespace",
        4: "package",
        5: "class",
        6: "method",
        7: "property",
        8: "field",
        9: "constructor",
        10: "enum",
        11: "interface",
        12: "function",
        13: "variable",
        14: "constant",
        22: "enum-member",
        23: "struct",
    }
    try:
        return labels.get(int(kind), "symbol")
    except (TypeError, ValueError):
        return "symbol"


def _format_locations(
    locations: list[Any],
    manager: LanguageServerManager,
) -> str:
    """Format multilspy ``Location`` dicts into ``<file>:<line>:<col>`` rows
    using 1-based coordinates."""
    rendered: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        path = (
            loc.get("absolutePath")
            or loc.get("relativePath")
            or loc.get("uri")
            or "<unknown>"
        )
        # Prefer a path relative to the repository root for readability.
        if isinstance(path, str) and os.path.isabs(path):
            try:
                path = os.path.relpath(path, start=manager.repository_root)
            except ValueError:
                pass
        range_ = loc.get("range") or {}
        start = range_.get("start") if isinstance(range_, dict) else None
        if isinstance(start, dict):
            line, col = line_col_0_to_1(
                int(start.get("line", 0) or 0),
                int(start.get("character", 0) or 0),
            )
            rendered.append(f"{path}:{line}:{col}")
        else:
            rendered.append(str(path))
    if not rendered:
        return "(no location data)"
    return "\n".join(rendered)


def _read_push_diagnostics(server: Any, relpath: str) -> list[Any]:
    """Best-effort read of multilspy's published-diagnostics cache.

    multilspy stashes the latest ``publishDiagnostics`` payload on the
    underlying ``LanguageServer`` instance. We probe the attributes it has
    historically exposed without crashing if they are absent — diagnostics
    is a soft feature on top of multilspy's public API.
    """
    candidates = (
        server,
        getattr(server, "language_server", None),
    )
    keys = (relpath, relpath.replace("\\", "/"))
    for candidate in candidates:
        if candidate is None:
            continue
        cache = getattr(candidate, "diagnostics", None)
        if not isinstance(cache, dict):
            cache = getattr(candidate, "_diagnostics", None)
        if isinstance(cache, dict):
            for key in keys:
                value = cache.get(key)
                if value is not None:
                    return list(value) if isinstance(value, list) else [value]
    return []


def _format_diagnostics(diagnostics: list[Any]) -> str:
    rows: list[str] = []
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        severity = _severity_label(diag.get("severity"))
        message = diag.get("message", "")
        range_ = diag.get("range") or {}
        start = range_.get("start") if isinstance(range_, dict) else None
        if isinstance(start, dict):
            line, _col = line_col_0_to_1(
                int(start.get("line", 0) or 0),
                int(start.get("character", 0) or 0),
            )
            rows.append(f"[{severity}] Line {line}: {message}")
        else:
            rows.append(f"[{severity}] {message}")
    if not rows:
        return "(no diagnostics)"
    return "\n".join(rows)


def _severity_label(severity: Any) -> str:
    labels = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}
    try:
        return labels.get(int(severity), "Diagnostic")
    except (TypeError, ValueError):
        return "Diagnostic"
