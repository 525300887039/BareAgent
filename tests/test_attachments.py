from __future__ import annotations

import sys
import types
from pathlib import Path

from bareagent.ui import attachments
from bareagent.ui.attachments import (
    build_attachment_prefix,
    extract_attachments,
    grab_clipboard_image,
)

# --- extract_attachments ---------------------------------------------------


def test_extract_single_marker():
    text, paths = extract_attachments("look at [image:.attach/a.png] please")
    assert paths == [".attach/a.png"]
    assert "image:" not in text
    assert text == "look at please"


def test_extract_multiple_markers_order_preserving():
    text, paths = extract_attachments("[image:a.png] and [image:b.png]")
    assert paths == ["a.png", "b.png"]
    assert text == "and"


def test_extract_dedups_repeated_path():
    _, paths = extract_attachments("[image:a.png] [image:a.png]")
    assert paths == ["a.png"]


def test_extract_no_marker_returns_text_unchanged():
    text, paths = extract_attachments("just a normal message")
    assert paths == []
    assert text == "just a normal message"


def test_extract_strips_whitespace_in_path():
    _, paths = extract_attachments("[image:  a.png  ]")
    assert paths == ["a.png"]


# --- build_attachment_prefix -----------------------------------------------


def test_build_prefix_empty():
    assert build_attachment_prefix([]) == ""


def test_build_prefix_multiple():
    prefix = build_attachment_prefix(["a.png", "b.png"])
    assert prefix.count("read_file") == 2
    assert "a.png" in prefix
    assert "b.png" in prefix
    assert prefix.count("\n") == 1


# --- grab_clipboard_image --------------------------------------------------


def _install_fake_pil(monkeypatch, grab_return, *, image_cls=None):
    """Install a fake PIL package with ImageGrab.grabclipboard -> grab_return."""

    class _FakeImage:
        def __init__(self):
            self.saved_to = None

        def save(self, dest):
            self.saved_to = dest
            Path(dest).write_bytes(b"fake-png")

    used_image_cls = image_cls or _FakeImage

    pil = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    image_mod.Image = used_image_cls  # type: ignore[attr-defined]
    imagegrab_mod = types.ModuleType("PIL.ImageGrab")
    imagegrab_mod.grabclipboard = lambda: grab_return  # type: ignore[attr-defined]
    pil.Image = image_mod  # type: ignore[attr-defined]
    pil.ImageGrab = imagegrab_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", imagegrab_mod)
    return used_image_cls


def test_grab_returns_none_when_pillow_missing(monkeypatch, tmp_path):
    # Force the lazy import to fail.
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert grab_clipboard_image(tmp_path, "p.png") is None


def test_grab_saves_pil_image(monkeypatch, tmp_path):
    image_cls = _install_fake_pil(monkeypatch, grab_return=None)
    # grab_return needs to be an instance of the Image class; build one now.
    instance = image_cls()
    _install_fake_pil(monkeypatch, grab_return=instance, image_cls=image_cls)

    result = grab_clipboard_image(tmp_path, "paste-1.png")

    assert result == tmp_path / "paste-1.png"
    assert result.exists()


def test_grab_copies_first_image_from_file_list(monkeypatch, tmp_path):
    src = tmp_path / "clip.png"
    src.write_bytes(b"src-bytes")
    _install_fake_pil(monkeypatch, grab_return=[str(tmp_path / "notes.txt"), str(src)])

    result = grab_clipboard_image(tmp_path / "dest", "paste-2.png")

    assert result == tmp_path / "dest" / "paste-2.png"
    assert result.read_bytes() == b"src-bytes"


def test_grab_returns_none_when_clipboard_empty(monkeypatch, tmp_path):
    _install_fake_pil(monkeypatch, grab_return=None)
    assert grab_clipboard_image(tmp_path, "p.png") is None


def test_grab_returns_none_on_grab_exception(monkeypatch, tmp_path):
    _install_fake_pil(monkeypatch, grab_return=None)
    # Replace grabclipboard with a raising one.
    sys.modules["PIL.ImageGrab"].grabclipboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert grab_clipboard_image(tmp_path, "p.png") is None


def test_image_exts_matches_file_read():
    from bareagent.core.handlers.file_read import _IMAGE_EXT_TO_MIME

    assert attachments.IMAGE_EXTS == frozenset(_IMAGE_EXT_TO_MIME.keys())
