from __future__ import annotations

import re
import shutil
from pathlib import Path

# Terminal image input (task 07-14-multimodal). Pure logic, no prompt-toolkit /
# main dependency so it stays unit-testable. Images must land INSIDE the
# workspace and be referenced by a relative path, because read_file's safe_path
# rejects absolute paths and confines reads to the workspace.

# Placeholder inserted into the input line on paste, e.g. "[image:.attach/p.png]".
_MARKER_RE = re.compile(r"\[image:([^\]]+)\]")

# Image extensions accepted for /attach and clipboard file paths. Mirrors
# core/handlers/file_read._IMAGE_EXT_TO_MIME.
IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def extract_attachments(text: str) -> tuple[str, list[str]]:
    """Strip ``[image:<path>]`` markers out of a submitted line.

    Returns ``(text_without_markers, paths)`` where ``paths`` is order-preserving
    and de-duplicated. Surrounding whitespace left by removed markers is
    collapsed so the cleaned text reads naturally.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for match in _MARKER_RE.finditer(text):
        path = match.group(1).strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    cleaned = _MARKER_RE.sub("", text)
    # Collapse runs of spaces/tabs left behind, but keep newlines.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, paths


def build_attachment_prefix(paths: list[str]) -> str:
    """Build the user-turn prefix that points the model at attached images.

    One line per path telling the model to view it with read_file. Empty input
    yields an empty string (no prefix).
    """
    if not paths:
        return ""
    return "\n".join(f"用户提供了图片 {path}，请用 read_file 查看。" for path in paths)


def grab_clipboard_image(dest_dir: Path, name: str) -> Path | None:
    """Save the clipboard image (if any) to ``dest_dir/name``; return its path.

    Uses Pillow's ImageGrab (optional ``[clipboard]`` extra), lazily imported so
    the app runs without it. Handles both shapes ImageGrab.grabclipboard()
    returns: a PIL Image (raw screenshot/paste) or a list of file paths
    (Windows/macOS file copy). Returns ``None`` when there is no image, Pillow is
    missing, or anything fails (fail-open).
    """
    try:
        from PIL import Image, ImageGrab  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001 - grabbing can fail on odd clipboard states
        return None

    if grabbed is None:
        return None

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(grabbed, list):
            # File paths copied to the clipboard: take the first image file.
            for entry in grabbed:
                src = Path(str(entry))
                if src.suffix.lower() in IMAGE_EXTS and src.is_file():
                    # File-list clipboard entries are copied byte-for-byte, so
                    # preserve their extension. read_file derives MIME from the
                    # suffix and must not label JPEG/GIF/WebP bytes as PNG.
                    dest = (dest_dir / name).with_suffix(src.suffix.lower())
                    shutil.copy(src, dest)
                    return dest
            return None
        if isinstance(grabbed, Image.Image):
            dest = dest_dir / name
            grabbed.save(dest)
            return dest
    except Exception:  # noqa: BLE001 - disk/encoding failures degrade to no-attach
        return None
    return None
