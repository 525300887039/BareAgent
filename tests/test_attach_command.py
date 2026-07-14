from __future__ import annotations

import itertools

from bareagent.main import _ATTACHMENT_DIRNAME, _handle_attach_command


def _counter():
    return itertools.count(1)


def test_attach_file_inside_workspace_referenced_directly(tmp_path):
    img = tmp_path / "sub" / "pic.png"
    img.parent.mkdir()
    img.write_bytes(b"png")

    rel, feedback = _handle_attach_command(
        f" {img}",
        workspace_path=tmp_path,
        attachment_dir=tmp_path / _ATTACHMENT_DIRNAME,
        counter=_counter(),
    )

    assert rel == "sub/pic.png"
    assert "Attached" in feedback
    # Not copied into the attachment dir.
    assert not (tmp_path / _ATTACHMENT_DIRNAME).exists()


def test_attach_file_outside_workspace_is_copied_in(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "external.jpg"
    outside.write_bytes(b"jpeg-bytes")
    attach_dir = workspace / _ATTACHMENT_DIRNAME

    rel, feedback = _handle_attach_command(
        str(outside),
        workspace_path=workspace,
        attachment_dir=attach_dir,
        counter=_counter(),
    )

    assert rel is not None
    assert rel.startswith(f"{_ATTACHMENT_DIRNAME}/")
    assert rel.endswith("external.jpg")
    copied = workspace / rel
    assert copied.read_bytes() == b"jpeg-bytes"


def test_attach_nonexistent_file_errors(tmp_path):
    rel, feedback = _handle_attach_command(
        "does/not/exist.png",
        workspace_path=tmp_path,
        attachment_dir=tmp_path / _ATTACHMENT_DIRNAME,
        counter=_counter(),
    )
    assert rel is None
    assert feedback.startswith("Error:")
    assert "not found" in feedback


def test_attach_non_image_errors(tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hi", encoding="utf-8")

    rel, feedback = _handle_attach_command(
        str(doc),
        workspace_path=tmp_path,
        attachment_dir=tmp_path / _ATTACHMENT_DIRNAME,
        counter=_counter(),
    )
    assert rel is None
    assert feedback.startswith("Error:")
    assert "unsupported image type" in feedback


def test_attach_empty_arg_shows_usage(tmp_path):
    rel, feedback = _handle_attach_command(
        "",
        workspace_path=tmp_path,
        attachment_dir=tmp_path / _ATTACHMENT_DIRNAME,
        counter=_counter(),
    )
    assert rel is None
    assert "Usage:" in feedback
