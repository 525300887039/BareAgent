"""Tests for ``src.lsp.workspace_edit`` — applying an LSP WorkspaceEdit to disk.

These are pure-function tests: they build ``WorkspaceEdit`` dicts by hand (both
the ``changes`` and ``documentChanges`` shapes) and assert the on-disk result,
without any multilspy / language-server involvement.
"""

from __future__ import annotations

from pathlib import Path

from src.lsp.coord import path_to_document_uri
from src.lsp.workspace_edit import apply_text_edits, apply_workspace_edit

# ---------------------------------------------------------------------------
# apply_text_edits — in-memory splicing
# ---------------------------------------------------------------------------


def _text_edit(
    start_line: int,
    start_char: int,
    end_line: int,
    end_char: int,
    new_text: str,
) -> dict:
    return {
        "range": {
            "start": {"line": start_line, "character": start_char},
            "end": {"line": end_line, "character": end_char},
        },
        "newText": new_text,
    }


def test_apply_single_edit() -> None:
    text = "foo = 1\n"
    edits = [_text_edit(0, 0, 0, 3, "bar")]
    assert apply_text_edits(text, edits) == "bar = 1\n"


def test_apply_multiple_edits_same_line_descending_order() -> None:
    # Two renames on the same line: ``foo`` (col 0-3) and ``foo`` (col 6-9).
    text = "foo + foo\n"
    edits = [
        _text_edit(0, 0, 0, 3, "bar"),
        _text_edit(0, 6, 0, 9, "bar"),
    ]
    assert apply_text_edits(text, edits) == "bar + bar\n"


def test_apply_edits_order_independent() -> None:
    # The result must not depend on the order edits arrive in — bottom-up
    # application guarantees earlier edits don't shift later offsets.
    text = "aaa\nbbb\nccc\n"
    forward = [
        _text_edit(0, 0, 0, 3, "XX"),
        _text_edit(2, 0, 2, 3, "YYYY"),
    ]
    reversed_edits = list(reversed(forward))
    assert apply_text_edits(text, forward) == apply_text_edits(text, reversed_edits)
    assert apply_text_edits(text, forward) == "XX\nbbb\nYYYY\n"


def test_apply_cross_line_range() -> None:
    text = "def foo(\n    a,\n):\n    pass\n"
    # Replace the signature spanning lines 0-2 with a single-line one.
    edits = [_text_edit(0, 4, 2, 1, "renamed(a)")]
    out = apply_text_edits(text, edits)
    assert out == "def renamed(a):\n    pass\n"


def test_apply_edit_without_trailing_newline() -> None:
    text = "foo = 1"  # no trailing newline
    edits = [_text_edit(0, 0, 0, 3, "bar")]
    assert apply_text_edits(text, edits) == "bar = 1"


def test_apply_edit_preserves_crlf() -> None:
    text = "foo\r\nfoo\r\n"
    edits = [
        _text_edit(0, 0, 0, 3, "bar"),
        _text_edit(1, 0, 1, 3, "bar"),
    ]
    assert apply_text_edits(text, edits) == "bar\r\nbar\r\n"


# ---------------------------------------------------------------------------
# apply_text_edits — UTF-16 ``character`` offsets vs Python code points
# ---------------------------------------------------------------------------


def test_apply_edit_after_astral_char_offset_correct() -> None:
    # An emoji is one Python str index but TWO UTF-16 code units. In
    # "<emoji> x = 1": emoji = UTF-16 units 0-1, space = unit 2, ``x`` = unit 3.
    # The server reports ``x`` at UTF-16 character 3; renaming x -> y must hit
    # the ``x``. A naive code-point reading would target unit 3 as Python col 3
    # (the ``=`` side) and corrupt the line.
    text = "\U0001f600 x = 1\n"
    edits = [_text_edit(0, 3, 0, 4, "y")]
    assert apply_text_edits(text, edits) == "\U0001f600 y = 1\n"


def test_apply_edit_multiple_astral_chars() -> None:
    # Two emoji before the symbol: 4 UTF-16 units (2 Python indices), space at
    # unit 4, ``x`` at UTF-16 char 5.
    text = "\U0001f600\U0001f601 x = 1\n"
    edits = [_text_edit(0, 5, 0, 6, "y")]
    assert apply_text_edits(text, edits) == "\U0001f600\U0001f601 y = 1\n"


def test_apply_edit_astral_across_lines() -> None:
    # Astral char on line 0 must not perturb line 1's offsets; line 1 also has
    # an emoji before its symbol. ``a``/``b`` sit at UTF-16 char 3 on each line.
    text = "\U0001f600 a = 1\n\U0001f602 b = 2\n"
    edits = [
        _text_edit(0, 3, 0, 4, "x"),  # line 0: rename a -> x
        _text_edit(1, 3, 1, 4, "y"),  # line 1: rename b -> y
    ]
    assert apply_text_edits(text, edits) == "\U0001f600 x = 1\n\U0001f602 y = 2\n"


def test_apply_edit_replace_the_astral_char_itself() -> None:
    # Selecting UTF-16 chars 0-2 covers exactly the single emoji code point.
    text = "\U0001f600x\n"
    edits = [_text_edit(0, 0, 0, 2, "Z")]
    assert apply_text_edits(text, edits) == "Zx\n"


def test_apply_edit_bmp_line_unchanged_regression() -> None:
    # Pure ASCII / BMP: UTF-16 units == Python indices, behavior unchanged.
    text = "héllo world\n"  # all BMP, including the accented e
    edits = [_text_edit(0, 6, 0, 11, "there")]
    assert apply_text_edits(text, edits) == "héllo there\n"


# ---------------------------------------------------------------------------
# apply_workspace_edit — ``changes`` form
# ---------------------------------------------------------------------------


def test_changes_form_single_file(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\nprint(foo)\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    workspace_edit = {
        "changes": {
            uri: [
                _text_edit(0, 0, 0, 3, "bar"),
                _text_edit(1, 6, 1, 9, "bar"),
            ]
        }
    }
    result = apply_workspace_edit(workspace_edit)
    assert result.changed_any
    assert result.total_edits == 2
    assert target.read_text(encoding="utf-8") == "bar = 1\nprint(bar)\n"
    # The summary keys by absolute path.
    (only_path,) = result.files
    assert Path(only_path) == target


# ---------------------------------------------------------------------------
# apply_workspace_edit — ``documentChanges`` (TextDocumentEdit) form
# ---------------------------------------------------------------------------


def test_document_changes_form_single_file(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    workspace_edit = {
        "documentChanges": [
            {
                "textDocument": {"uri": uri, "version": 1},
                "edits": [_text_edit(0, 0, 0, 3, "bar")],
            }
        ]
    }
    result = apply_workspace_edit(workspace_edit)
    assert result.total_edits == 1
    assert target.read_text(encoding="utf-8") == "bar = 1\n"


# ---------------------------------------------------------------------------
# Cross-file rename
# ---------------------------------------------------------------------------


def test_cross_file_rename(tmp_path: Path) -> None:
    defn = tmp_path / "good.py"
    defn.write_text("def foo():\n    return 1\n", encoding="utf-8")
    usage = tmp_path / "use.py"
    usage.write_text("from good import foo\nfoo()\n", encoding="utf-8")

    workspace_edit = {
        "changes": {
            path_to_document_uri(str(defn)): [_text_edit(0, 4, 0, 7, "bar")],
            path_to_document_uri(str(usage)): [
                _text_edit(0, 17, 0, 20, "bar"),
                _text_edit(1, 0, 1, 3, "bar"),
            ],
        }
    }
    result = apply_workspace_edit(workspace_edit)
    assert len(result.files) == 2
    assert result.total_edits == 3
    assert defn.read_text(encoding="utf-8") == "def bar():\n    return 1\n"
    assert usage.read_text(encoding="utf-8") == "from good import bar\nbar()\n"


# ---------------------------------------------------------------------------
# Resource operations are skipped (MVP does not do file-level renames)
# ---------------------------------------------------------------------------


def test_resource_operations_are_skipped(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    new_uri = path_to_document_uri(str(tmp_path / "renamed.py"))
    workspace_edit = {
        "documentChanges": [
            {"kind": "rename", "oldUri": uri, "newUri": new_uri},
            {"kind": "create", "uri": new_uri},
            {"kind": "delete", "uri": uri},
            {
                "textDocument": {"uri": uri},
                "edits": [_text_edit(0, 0, 0, 3, "bar")],
            },
        ]
    }
    result = apply_workspace_edit(workspace_edit)
    # The text edit still applied...
    assert target.read_text(encoding="utf-8") == "bar = 1\n"
    assert result.total_edits == 1
    # ...and all three resource ops were recorded as skipped, not performed.
    assert len(result.skipped) == 3
    assert not (tmp_path / "renamed.py").exists()
    assert target.exists()


# ---------------------------------------------------------------------------
# documentChanges takes precedence: ``changes`` is ignored when both present
# ---------------------------------------------------------------------------


def test_document_changes_wins_over_changes_no_double_apply(tmp_path: Path) -> None:
    # A server that gives both forms for the same URI (LSP back-compat fallback)
    # must not have the edit applied twice. With documentChanges present, the
    # ``changes`` form is ignored entirely, so the splice runs once.
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    edit = _text_edit(0, 0, 0, 3, "bar")
    workspace_edit = {
        "documentChanges": [
            {"textDocument": {"uri": uri, "version": 1}, "edits": [edit]},
        ],
        "changes": {uri: [edit]},
    }
    result = apply_workspace_edit(workspace_edit)
    # Applied once: 3-char "foo" -> "bar". A double splice would corrupt this.
    assert target.read_text(encoding="utf-8") == "bar = 1\n"
    assert result.total_edits == 1


def test_changes_only_still_applied_regression(tmp_path: Path) -> None:
    # No documentChanges: the ``changes`` fallback is parsed and applied.
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    workspace_edit = {"changes": {uri: [_text_edit(0, 0, 0, 3, "bar")]}}
    result = apply_workspace_edit(workspace_edit)
    assert target.read_text(encoding="utf-8") == "bar = 1\n"
    assert result.total_edits == 1


def test_document_changes_only_still_applied_regression(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    uri = path_to_document_uri(str(target))
    workspace_edit = {
        "documentChanges": [
            {"textDocument": {"uri": uri}, "edits": [_text_edit(0, 0, 0, 3, "bar")]},
        ]
    }
    result = apply_workspace_edit(workspace_edit)
    assert target.read_text(encoding="utf-8") == "bar = 1\n"
    assert result.total_edits == 1


def test_empty_workspace_edit_changes_nothing(tmp_path: Path) -> None:
    result = apply_workspace_edit({})
    assert not result.changed_any
    assert result.total_edits == 0


def test_unreadable_uri_is_skipped_not_raised(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.py"  # never created
    uri = path_to_document_uri(str(missing))
    workspace_edit = {"changes": {uri: [_text_edit(0, 0, 0, 3, "bar")]}}
    result = apply_workspace_edit(workspace_edit)
    assert not result.changed_any
    assert result.skipped  # a "could not read" note was recorded
