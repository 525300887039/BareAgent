from __future__ import annotations

import html.parser
import re
from http.client import HTTPResponse
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from bareagent.core.handlers.file_read import (
    _IMAGE_DISABLED_ERROR,
    _IMAGE_EXT_TO_MIME,
    _MAX_IMAGE_BYTES,
    build_image_blocks,
)

_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_LENGTH = 10000
_USER_AGENT = "BareAgent/1.0"
_RE_WHITESPACE = re.compile(r"[ \t]+")

# The image mime types web_fetch can turn into image blocks — the value set of
# the local-file whitelist, so the two paths never drift.
_ALLOWED_IMAGE_MIMES = frozenset(_IMAGE_EXT_TO_MIME.values())


class _HTMLToText(html.parser.HTMLParser):
    """将 HTML 转为可读纯文本。

    - 跳过 <script>、<style>、<nav>、<footer>、<header>、<noscript> 标签内容
    - 在块级元素（p/div/h1-h6/li/br/tr）处插入换行
    - 合并连续空白
    """

    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header", "noscript"})
    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "br",
            "tr",
            "blockquote",
            "pre",
            "section",
            "article",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        result_lines: list[str] = []
        prev_empty = False
        for line in raw.splitlines():
            stripped = _RE_WHITESPACE.sub(" ", line).strip()
            if not stripped:
                if not prev_empty:
                    result_lines.append("")
                prev_empty = True
            else:
                result_lines.append(stripped)
                prev_empty = False
        return "\n".join(result_lines).strip()


def html_to_text(html_content: str) -> str:
    """将 HTML 字符串转为可读纯文本。"""
    parser = _HTMLToText()
    parser.feed(html_content)
    return parser.get_text()


def _truncate(text: str, max_length: int) -> str:
    """截断文本到指定长度，在最后一个完整行处截断。"""
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    # 尝试在最后一个换行处截断
    last_newline = truncated.rfind("\n")
    if last_newline > max_length * 0.8:
        truncated = truncated[:last_newline]
    return truncated + "\n\n[... content truncated]"


def run_web_fetch(
    url: str,
    max_length: int = _DEFAULT_MAX_LENGTH,
    timeout: int = _DEFAULT_TIMEOUT,
    *,
    image_enabled: bool = True,
) -> str | list[dict[str, Any]]:
    """Fetch content from a URL.

    - ``image/*`` Content-Type -> ``[text, image]`` blocks when the type is in
      the supported whitelist (png/jpeg/gif/webp) and the model has vision;
      otherwise a friendly Error. ``image_enabled`` defaults to True so existing
      callers are byte-for-byte unchanged.
    - Everything else -> HTML-to-text (or raw text), truncated.
    """
    if not url.startswith(("http://", "https://")):
        return f"Error: URL must start with http:// or https:// (got: {url})"

    request = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as resp:  # noqa: S310
            content_type = resp.headers.get("Content-Type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime.startswith("image/"):
                return _fetch_image(resp, mime, url, image_enabled)

            charset = resp.headers.get_content_charset() or "utf-8"
            raw_bytes = resp.read(max_length * 4)
            body = raw_bytes.decode(charset, errors="replace")
    except (URLError, OSError, TimeoutError) as exc:
        return f"Error fetching URL: {exc}"
    except ValueError as exc:
        return f"Error: invalid URL: {exc}"

    if "html" in content_type.lower():
        text = html_to_text(body)
    else:
        text = body

    return _truncate(text, max_length)


def _fetch_image(
    resp: HTTPResponse,
    mime: str,
    url: str,
    image_enabled: bool,
) -> str | list[dict[str, Any]]:
    """Turn an image response into ``[text, image]`` blocks, or a friendly Error."""
    if mime not in _ALLOWED_IMAGE_MIMES:
        return (
            f"Error: unsupported image type {mime!r} at {url}. Supported: "
            "png/jpeg/gif/webp. Download it and process it locally."
        )
    if not image_enabled:
        return _IMAGE_DISABLED_ERROR
    # Read one byte past the cap so an over-limit image is detected without
    # buffering the whole payload.
    raw = resp.read(_MAX_IMAGE_BYTES + 1)
    if len(raw) > _MAX_IMAGE_BYTES:
        return (
            f"Error: image at {url} exceeds the {_MAX_IMAGE_BYTES} byte limit. "
            "Download it and process it locally."
        )
    return build_image_blocks(raw, mime, url)
