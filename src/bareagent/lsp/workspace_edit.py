"""Apply an LSP ``WorkspaceEdit`` to disk.

A ``WorkspaceEdit`` is what a language server returns from
``textDocument/rename``. It can carry edits in two shapes (LSP 3.x):

* ``changes`` — ``{uri: [TextEdit, ...]}`` (the legacy map form).
* ``documentChanges`` — an ordered list whose items are either
  ``TextDocumentEdit`` (``{"textDocument": {"uri": ...}, "edits": [...]}``) or
  *resource operations* (``CreateFile`` / ``RenameFile`` / ``DeleteFile``,
  distinguished by a ``"kind"`` field). The semantic-rename MVP does **not**
  perform file-level operations, so resource operations are collected into a
  ``skipped`` list and surfaced to the caller rather than applied.

This module is intentionally free of any multilspy / LSP-client dependency so
it can be unit-tested with plain dicts. It only reads the file from disk,
applies ``TextEdit`` ranges, and writes back via
:func:`bareagent.core.fileutil.atomic_write_text`.

Coordinates inside a ``TextEdit`` ``range`` are 0-based ``(line, character)``
in LSP wire form. Multiple edits to the same file are applied **bottom-up**
(sorted by start position descending) so earlier edits never shift the
character offsets of later ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bareagent.core.fileutil import atomic_write_text

from .coord import document_uri_to_path


@dataclass(slots=True)
class WorkspaceEditResult:
    """Outcome of applying a ``WorkspaceEdit``.

    ``files`` maps an absolute (native) file path to the number of ``TextEdit``
    entries applied to it. ``skipped`` holds human-readable descriptions of any
    resource operations (CreateFile / RenameFile / DeleteFile) that the MVP did
    not perform.
    """

    files: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def total_edits(self) -> int:
        return sum(self.files.values())

    @property
    def changed_any(self) -> bool:
        return bool(self.files)


def _iter_edit_groups(
    workspace_edit: dict[str, Any],
    skipped: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize a WorkspaceEdit into ``{uri: [TextEdit, ...]}``.

    A ``WorkspaceEdit`` may carry edits in ``changes`` *and/or*
    ``documentChanges``. The LSP spec recommends a client that understands
    ``documentChanges`` use it **exclusively** when present and ignore
    ``changes`` entirely — ``changes`` is only the backward-compatibility
    fallback for clients that don't. Merging both would apply the same edit
    twice when a server emits both forms for one URI, and the bottom-up splice
    would then corrupt the file. So we choose one form, never merge.

    Resource operations inside ``documentChanges`` (items carrying a ``"kind"``
    field) are recorded in ``skipped`` and not returned for application.
    """
    groups: dict[str, list[dict[str, Any]]] = {}

    document_changes = workspace_edit.get("documentChanges")
    if isinstance(document_changes, list):
        # documentChanges present: parse it and ignore ``changes`` entirely.
        for item in document_changes:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind in ("create", "rename", "delete"):
                # Resource operation — MVP does not do file-level renames.
                skipped.append(_describe_resource_op(kind, item))
                continue
            text_document = item.get("textDocument")
            uri = (
                text_document.get("uri")
                if isinstance(text_document, dict)
                else None
            )
            edits = item.get("edits")
            if not isinstance(uri, str) or not isinstance(edits, list):
                continue
            groups.setdefault(uri, []).extend(
                edit for edit in edits if isinstance(edit, dict)
            )
        return groups

    changes = workspace_edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            if not isinstance(uri, str) or not isinstance(edits, list):
                continue
            groups.setdefault(uri, []).extend(
                edit for edit in edits if isinstance(edit, dict)
            )

    return groups


def _describe_resource_op(kind: str, item: dict[str, Any]) -> str:
    """Best-effort one-line description of a skipped resource operation."""
    if kind == "rename":
        old = item.get("oldUri", "?")
        new = item.get("newUri", "?")
        return f"rename {old} -> {new}"
    uri = item.get("uri", "?")
    return f"{kind} {uri}"


def _edit_sort_key(edit: dict[str, Any]) -> tuple[int, int]:
    """Sort key from a TextEdit's ``range.start`` (0-based line, character)."""
    range_ = edit.get("range") or {}
    start = range_.get("start") if isinstance(range_, dict) else None
    if not isinstance(start, dict):
        return (0, 0)
    line = int(start.get("line", 0) or 0)
    char = int(start.get("character", 0) or 0)
    return (line, char)


def _utf16_units_to_py_col(line_text: str, utf16_char: int) -> int:
    """Map a UTF-16 code-unit offset into a line to a Python str column index.

    LSP ``Position.character`` is counted in **UTF-16 code units**, not Python
    code points. multilspy 0.0.15 does not negotiate ``positionEncoding`` with
    the server, so the default (UTF-16) is what every server uses. A non-BMP
    character (``ord(ch) > 0xFFFF`` — emoji, astral plane) is a surrogate pair
    occupying *two* UTF-16 units but a *single* Python str index. Treating the
    character offset as a Python index therefore shifts every position after an
    astral character left by one per astral char, silently corrupting the edit
    range and the file.

    We walk the line accumulating UTF-16 units until we reach ``utf16_char`` and
    return the corresponding Python column. A ``utf16_char`` that lands in the
    middle of a surrogate pair, or runs past the line's UTF-16 length, clamps to
    the line end — matching the boundary-clamp safety of the offset path.

    (The read-only ``lsp_definition`` / ``lsp_references`` coordinates in
    ``coord.py`` pass ``character`` through unconverted; that only affects
    display, so the small risk is out of scope here — only the write path can
    corrupt a file.)
    """
    if utf16_char <= 0:
        return 0
    units = 0
    for col, ch in enumerate(line_text):
        if units >= utf16_char:
            return col
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(line_text)


def _offset_for_position(
    line_starts: list[int],
    lines: list[str],
    line: int,
    char: int,
    text_len: int,
) -> int:
    """Convert a 0-based ``(line, character)`` to an absolute string offset.

    ``line_starts[i]`` is the offset where line ``i`` begins and ``lines[i]`` is
    that line's text (terminator stripped) used to translate the UTF-16
    ``character`` into a Python column. Positions past the end of a line / file
    are clamped to the file length so a malformed range from the server can
    never raise — it just edits at the boundary.
    """
    if not line_starts:
        return 0
    if line < 0:
        line = 0
    if line >= len(line_starts):
        return text_len
    line_text = lines[line] if line < len(lines) else ""
    py_col = _utf16_units_to_py_col(line_text, char)
    return min(line_starts[line] + py_col, text_len)


def _build_line_starts(text: str) -> list[int]:
    """Return the absolute offset at which each line begins.

    Uses the same line model the LSP spec implies: a line ends at (and includes)
    its terminator, and the next line starts immediately after. The final
    sentinel lets a position on the last line resolve even when the file has no
    trailing newline.
    """
    starts = [0]
    for index, ch in enumerate(text):
        if ch == "\n":
            starts.append(index + 1)
    return starts


def _build_lines(text: str) -> list[str]:
    """Split ``text`` into per-line content for UTF-16 column translation.

    Aligned with :func:`_build_line_starts`: one entry per line, split on
    ``\\n``. A trailing ``\\r`` is kept (CRLF files) so UTF-16 unit counting
    matches the bytes actually on the line; the splice still uses absolute
    offsets, so the terminator is never disturbed.
    """
    return text.split("\n")


def apply_text_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """Apply a list of LSP ``TextEdit`` objects to ``text`` and return the result.

    Edits are applied bottom-up (sorted by start position descending) so the
    character offsets computed for earlier edits remain valid while later ones
    are spliced in. This yields the same result as applying every edit against
    the original document simultaneously, which is the LSP contract for a
    single ``TextEdit[]`` (the spec forbids overlapping ranges).
    """
    line_starts = _build_line_starts(text)
    lines = _build_lines(text)
    text_len = len(text)
    # Descending by start position: apply the last edit in the file first so
    # splicing it never shifts offsets of edits that come earlier.
    ordered = sorted(edits, key=_edit_sort_key, reverse=True)
    result = text
    for edit in ordered:
        range_ = edit.get("range") or {}
        start = range_.get("start") if isinstance(range_, dict) else None
        end = range_.get("end") if isinstance(range_, dict) else None
        new_text = edit.get("newText", "")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        start_off = _offset_for_position(
            line_starts,
            lines,
            int(start.get("line", 0) or 0),
            int(start.get("character", 0) or 0),
            text_len,
        )
        end_off = _offset_for_position(
            line_starts,
            lines,
            int(end.get("line", 0) or 0),
            int(end.get("character", 0) or 0),
            text_len,
        )
        if end_off < start_off:
            start_off, end_off = end_off, start_off
        result = result[:start_off] + str(new_text) + result[end_off:]
    return result


def apply_workspace_edit(workspace_edit: dict[str, Any]) -> WorkspaceEditResult:
    """Apply a full ``WorkspaceEdit`` to disk and return a summary.

    Parses both ``changes`` and ``documentChanges`` forms, groups the
    ``TextEdit`` entries by URI, applies each group bottom-up, and writes the
    result atomically. Resource operations (CreateFile / RenameFile /
    DeleteFile) are skipped and reported. A URI that resolves to a non-``file:``
    target, or whose file cannot be read, is skipped with a note rather than
    raising — the caller turns an empty result into an explicit error.
    """
    result = WorkspaceEditResult()
    groups = _iter_edit_groups(workspace_edit, result.skipped)

    for uri, edits in groups.items():
        if not edits:
            continue
        path = document_uri_to_path(uri)
        if path.startswith("file:") or "://" in path:
            # document_uri_to_path returns non-``file:`` URIs unchanged; we
            # cannot write those (untitled / virtual docs).
            result.skipped.append(f"unsupported document URI: {uri}")
            continue
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                original = handle.read()
        except OSError as exc:
            result.skipped.append(f"could not read {path}: {exc}")
            continue
        updated = apply_text_edits(original, edits)
        if updated != original:
            atomic_write_text_path(path, updated)
        result.files[path] = len(edits)

    return result


def atomic_write_text_path(path: str, text: str) -> None:
    """Thin shim so :func:`apply_workspace_edit` can write a ``str`` path.

    :func:`bareagent.core.fileutil.atomic_write_text` takes a ``Path``; constructing
    it here keeps the import surface of this module to one helper.
    """
    from pathlib import Path

    atomic_write_text(Path(path), text)
