"""read_file multimodal dispatch: images, notebooks, PDFs + text regression.

Covers ROADMAP 1.3: ``run_read`` returns ``[text, image]`` content blocks for
images, rendered text for notebooks/PDFs, and keeps the legacy line-numbered
text path for everything else.
"""

from __future__ import annotations

import base64
import builtins
import json

import pytest

from src.core.handlers import file_read
from src.core.handlers.file_read import _parse_page_range, run_read

# 1x1 transparent PNG. Decoding this inline avoids checking a binary fixture in.
_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_1X1 = base64.b64decode(_PNG_1X1_B64)


# --- images ----------------------------------------------------------------


def test_read_png_returns_image_blocks(tmp_path):
    (tmp_path / "pixel.png").write_bytes(_PNG_1X1)

    result = run_read(file_path="pixel.png", workspace=tmp_path)

    assert isinstance(result, list)
    assert len(result) == 2
    text_block, image_block = result
    assert text_block["type"] == "text"
    assert "pixel.png" in text_block["text"]
    assert "image/png" in text_block["text"]
    assert image_block["type"] == "image"
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(_PNG_1X1).decode("ascii"),
    }


@pytest.mark.parametrize(
    ("name", "expected_mime"),
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("A.PNG", "image/png"),
    ],
)
def test_image_extension_mime_mapping(tmp_path, name, expected_mime):
    (tmp_path / name).write_bytes(_PNG_1X1)

    result = run_read(file_path=name, workspace=tmp_path)

    assert isinstance(result, list)
    assert result[1]["source"]["media_type"] == expected_mime


def test_image_over_size_limit_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(file_read, "_MAX_IMAGE_BYTES", 10)
    (tmp_path / "big.png").write_bytes(_PNG_1X1)

    result = run_read(file_path="big.png", workspace=tmp_path)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "exceeding" in result


# --- notebooks -------------------------------------------------------------


def _notebook(cells: list[dict]) -> str:
    return json.dumps({"cells": cells, "metadata": {}, "nbformat": 4})


def test_read_notebook_renders_cells(tmp_path):
    nb = _notebook(
        [
            {"cell_type": "markdown", "source": ["# Title\n", "text"]},
            {
                "cell_type": "code",
                "source": ["print('hi')"],
                "outputs": [
                    {"output_type": "stream", "text": ["hi\n"]},
                    {
                        "output_type": "execute_result",
                        "data": {"text/plain": ["42"]},
                    },
                ],
            },
        ]
    )
    (tmp_path / "nb.ipynb").write_text(nb, encoding="utf-8")

    result = run_read(file_path="nb.ipynb", workspace=tmp_path)

    assert isinstance(result, str)
    assert "## Markdown cell 1" in result
    assert "# Title" in result
    assert "## Code cell 2" in result
    assert "print('hi')" in result
    assert "hi" in result
    assert "42" in result


def test_notebook_error_traceback_rendered(tmp_path):
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": ["raise ValueError('boom')"],
                "outputs": [
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "boom",
                        "traceback": ["Traceback...", "ValueError: boom"],
                    }
                ],
            }
        ]
    )
    (tmp_path / "err.ipynb").write_text(nb, encoding="utf-8")

    result = run_read(file_path="err.ipynb", workspace=tmp_path)

    assert "ValueError: boom" in result


def test_notebook_long_output_truncated(tmp_path):
    long_text = "x" * 5000
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": ["loop()"],
                "outputs": [{"output_type": "stream", "text": [long_text]}],
            }
        ]
    )
    (tmp_path / "long.ipynb").write_text(nb, encoding="utf-8")

    result = run_read(file_path="long.ipynb", workspace=tmp_path)

    assert isinstance(result, str)
    assert "... [truncated]" in result
    assert result.count("x") <= file_read._MAX_CELL_OUTPUT_CHARS + 10


def test_notebook_invalid_json_returns_error(tmp_path):
    (tmp_path / "bad.ipynb").write_text("{not json", encoding="utf-8")

    result = run_read(file_path="bad.ipynb", workspace=tmp_path)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "valid JSON" in result


# --- PDF page range parsing (pure function) --------------------------------


@pytest.mark.parametrize(
    ("pages", "total", "expected"),
    [
        (None, 3, [0, 1, 2]),
        ("", 3, [0, 1, 2]),
        ("2", 3, [1]),
        ("1-2", 3, [0, 1]),
        ("1-10", 3, [0, 1, 2]),  # end clamped
    ],
)
def test_parse_page_range_valid(pages, total, expected):
    assert _parse_page_range(pages, total) == expected


@pytest.mark.parametrize(
    "pages",
    ["abc", "0", "5", "3-1", "1-x"],
)
def test_parse_page_range_invalid(pages):
    result = _parse_page_range(pages, 3)
    assert isinstance(result, str)
    assert result.startswith("Error:")


# --- PDF library-missing degradation ---------------------------------------


def test_read_pdf_missing_library_returns_friendly_error(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = run_read(file_path="doc.pdf", workspace=tmp_path)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "[pdf]" in result


def test_read_pdf_extracts_text(tmp_path):
    pypdf = pytest.importorskip("pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    pdf_path = tmp_path / "blank.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)

    result = run_read(file_path="blank.pdf", workspace=tmp_path)
    assert isinstance(result, str)
    assert "--- Page 1 ---" in result
    assert "--- Page 2 ---" in result

    single = run_read(file_path="blank.pdf", pages="2", workspace=tmp_path)
    assert "--- Page 2 ---" in single
    assert "--- Page 1 ---" not in single


# --- text path regression --------------------------------------------------


def test_text_path_offset_limit_unchanged(tmp_path):
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    assert run_read(file_path="f.txt", workspace=tmp_path) == "1: alpha\n2: beta\n3: gamma"
    assert run_read(file_path="f.txt", offset=1, limit=1, workspace=tmp_path) == "2: beta"


def test_unknown_extension_uses_text_path(tmp_path):
    (tmp_path / "data.weird").write_text("one\ntwo", encoding="utf-8")

    result = run_read(file_path="data.weird", workspace=tmp_path)

    assert result == "1: one\n2: two"
