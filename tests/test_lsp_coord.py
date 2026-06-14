"""Tests for ``src.lsp.coord`` — coordinate + URI helpers."""

from __future__ import annotations

import os

import pytest

from bareagent.lsp.coord import (
    document_uri_to_path,
    line_col_0_to_1,
    line_col_1_to_0,
    path_to_document_uri,
    to_repo_relative,
)


def test_line_col_round_trip() -> None:
    assert line_col_1_to_0(1, 1) == (0, 0)
    assert line_col_1_to_0(10, 5) == (9, 4)
    assert line_col_0_to_1(0, 0) == (1, 1)
    assert line_col_0_to_1(9, 4) == (10, 5)


def test_line_col_clamps_invalid() -> None:
    # 0 / negative inputs clamp to 0 in 0-based form.
    assert line_col_1_to_0(0, 0) == (0, 0)
    assert line_col_1_to_0(-3, -3) == (0, 0)


def test_path_to_document_uri_round_trip(tmp_path) -> None:
    target = tmp_path / "foo.py"
    target.write_text("x = 1")
    uri = path_to_document_uri(str(target))
    assert uri.startswith("file://")
    back = document_uri_to_path(uri)
    # On Windows the path representation may use forward slashes; normalise
    # both sides to the OS-native form before comparing.
    assert os.path.normcase(os.path.normpath(back)) == os.path.normcase(
        os.path.normpath(str(target))
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific URI form")
def test_windows_uri_drive_letter_upper() -> None:
    uri = path_to_document_uri("D:\\code\\foo.py")
    # multilspy-compatible Windows form: file:///D:/code/foo.py (no %3A).
    assert "%3A" not in uri
    assert uri.startswith("file:///")


def test_document_uri_to_path_returns_non_file_unchanged() -> None:
    assert document_uri_to_path("http://example.com") == "http://example.com"


def test_to_repo_relative(tmp_path) -> None:
    inside = tmp_path / "sub" / "x.py"
    inside.parent.mkdir()
    inside.write_text("y = 1")
    rel = to_repo_relative(str(inside), str(tmp_path))
    assert rel == os.path.join("sub", "x.py")
