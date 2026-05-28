"""Coordinate and URI helpers shared by every LSP tool handler.

Tools take 1-based (line, column) from the LLM (matching editor convention)
and convert to 0-based for LSP requests. Results are converted back to
1-based before they reach the model.

The URI helpers prefer multilspy's ``PathUtils`` when available; otherwise
they fall back to a hand-written ``file:///`` construction that mirrors the
forms multilspy / the major language servers accept (Windows drive letters
upper-cased, no ``%3A`` encoding of the colon).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


def line_col_1_to_0(line: int, col: int) -> tuple[int, int]:
    """Convert 1-based editor coordinates to 0-based LSP coordinates.

    Position (1, 1) (the first character of the first line) maps to
    (0, 0). Values < 1 are clamped to 0 — LSP servers tolerate clamping
    better than they tolerate negative indices.
    """
    return (max(line - 1, 0), max(col - 1, 0))


def line_col_0_to_1(line: int, col: int) -> tuple[int, int]:
    """Convert 0-based LSP coordinates back to 1-based editor coordinates."""
    return (line + 1, col + 1)


def path_to_document_uri(path: str) -> str:
    """Convert a filesystem path to a ``file://`` URI.

    Prefers multilspy's ``PathUtils.path_to_uri`` when available so the URI
    shape matches what multilspy internally sends to the server. The fallback
    constructs ``file:///<abs-path>`` directly, leaving Windows drive letters
    upper-cased and the colon un-encoded (the form rust-analyzer, pyright,
    gopls all accept).
    """
    helper = _multilspy_path_to_uri()
    if helper is not None:
        try:
            return helper(path)
        except Exception:
            pass  # fall through to manual construction

    abs_path = os.path.abspath(path)
    # Normalize separators to forward slashes for the URI form.
    normalized = abs_path.replace("\\", "/")
    if normalized.startswith("/"):
        # POSIX absolute path: file:///abs/path
        return f"file://{normalized}"
    # Windows: D:/code/... → file:///D:/code/...
    return f"file:///{normalized}"


def document_uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI back to a native filesystem path.

    Handles both ``file:///D:/foo`` and ``file:///D%3A/foo`` (percent-encoded
    drive letter) forms. Non-``file:`` URIs are returned unchanged so callers
    can decide how to handle remote / virtual documents.
    """
    if not uri.startswith("file:"):
        return uri
    parsed = urlparse(uri)
    raw = unquote(parsed.path)
    # On Windows ``urlparse('file:///D:/foo').path`` yields ``/D:/foo``; strip
    # the leading slash so the result is a real native path.
    if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return raw


def to_repo_relative(path: str, repository_root: str) -> str:
    """Convert an absolute or relative file path to a path relative to the
    repository root.

    multilspy's request_* methods expect a relative path. If ``path`` is
    already relative, return it unchanged. If it cannot be made relative to
    ``repository_root`` (e.g. on a different drive), return the original
    ``path`` and let multilspy report the error.
    """
    try:
        return os.path.relpath(path, start=repository_root)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# multilspy path-utils delegation
# ---------------------------------------------------------------------------


def _multilspy_path_to_uri():  # pragma: no cover — exercised only when multilspy is present
    """Return ``multilspy.PathUtils.path_to_uri`` (or None) without crashing.

    Kept in a helper so the import is attempted lazily and never raises out
    to the caller. multilspy's exact module path differs across versions;
    we probe the most common ones.
    """
    try:
        from multilspy.multilspy_utils import PathUtils  # type: ignore
    except Exception:
        return None
    helper = getattr(PathUtils, "path_to_uri", None)
    if not callable(helper):
        return None

    def _bound(path: str) -> str:
        return helper(str(Path(path).resolve()))

    return _bound
