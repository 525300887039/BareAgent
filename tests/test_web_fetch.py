from __future__ import annotations

import base64

from bareagent.core.handlers import web_fetch
from bareagent.core.handlers.web_fetch import _truncate, html_to_text, run_web_fetch


class _FakeHeaders:
    def __init__(self, content_type: str, charset: str | None = "utf-8") -> None:
        self._content_type = content_type
        self._charset = charset

    def get(self, key: str, default: str = "") -> str:
        return self._content_type if key == "Content-Type" else default

    def get_content_charset(self) -> str | None:
        return self._charset


class _FakeResponse:
    def __init__(self, content_type: str, body: bytes, charset: str | None = "utf-8") -> None:
        self.headers = _FakeHeaders(content_type, charset)
        self._body = body

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _patch_response(monkeypatch, content_type: str, body: bytes, charset: str = "utf-8") -> None:
    monkeypatch.setattr(
        web_fetch,
        "urlopen",
        lambda request, timeout: _FakeResponse(content_type, body, charset),
    )


class TestHtmlToText:
    def test_plain_text_passthrough(self):
        assert html_to_text("hello world") == "hello world"

    def test_strips_script_and_style(self):
        html = "<p>before</p><script>alert(1)</script><style>.x{}</style><p>after</p>"
        text = html_to_text(html)
        assert "alert" not in text
        assert ".x{}" not in text
        assert "before" in text
        assert "after" in text

    def test_block_tags_add_newlines(self):
        html = "<p>first</p><p>second</p>"
        text = html_to_text(html)
        assert "first" in text
        assert "second" in text
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == 2

    def test_nested_skip_tags(self):
        html = "<nav><div><a>link</a></div></nav><p>content</p>"
        text = html_to_text(html)
        assert "link" not in text
        assert "content" in text

    def test_whitespace_collapse(self):
        html = "<p>  lots   of   spaces  </p>"
        text = html_to_text(html)
        assert "lots of spaces" in text


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("short", 100) == "short"

    def test_long_text_truncated(self):
        text = "a" * 200
        result = _truncate(text, 100)
        assert len(result) < 200
        assert "[... content truncated]" in result

    def test_truncate_at_newline(self):
        text = "line1\n" + "x" * 50 + "\nline3\n" + "y" * 200
        result = _truncate(text, 80)
        assert "[... content truncated]" in result


class TestRunWebFetch:
    def test_invalid_scheme(self):
        result = run_web_fetch("ftp://example.com")
        assert "Error" in result
        assert "http://" in result

    def test_invalid_url(self):
        result = run_web_fetch("not-a-url")
        assert "Error" in result

    def test_html_content_type_unchanged(self, monkeypatch):
        _patch_response(monkeypatch, "text/html; charset=utf-8", b"<p>hello</p>")
        result = run_web_fetch("https://example.com")
        assert result == "hello"

    def test_plain_text_content_type(self, monkeypatch):
        _patch_response(monkeypatch, "text/plain", b"just text")
        result = run_web_fetch("https://example.com/x.txt")
        assert result == "just text"


class TestWebFetchImage:
    def test_supported_image_with_vision_returns_blocks(self, monkeypatch):
        body = b"\x89PNG\r\n\x1a\nfake"
        _patch_response(monkeypatch, "image/png", body)
        result = run_web_fetch("https://example.com/a.png")
        assert isinstance(result, list)
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"
        assert result[1]["source"]["media_type"] == "image/png"
        assert result[1]["source"]["data"] == base64.b64encode(body).decode("ascii")

    def test_content_type_with_charset_param_parsed(self, monkeypatch):
        _patch_response(monkeypatch, "image/jpeg; charset=binary", b"jpegbytes")
        result = run_web_fetch("https://example.com/a.jpg")
        assert isinstance(result, list)
        assert result[1]["source"]["media_type"] == "image/jpeg"

    def test_no_vision_returns_gate_error(self, monkeypatch):
        _patch_response(monkeypatch, "image/png", b"pngbytes")
        result = run_web_fetch("https://example.com/a.png", image_enabled=False)
        assert isinstance(result, str)
        assert "vision" in result

    def test_unsupported_image_type_returns_hint(self, monkeypatch):
        _patch_response(monkeypatch, "image/svg+xml", b"<svg/>")
        result = run_web_fetch("https://example.com/a.svg")
        assert isinstance(result, str)
        assert "unsupported image type" in result

    def test_oversize_image_returns_hint(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "_MAX_IMAGE_BYTES", 8)
        _patch_response(monkeypatch, "image/png", b"x" * 20)
        result = run_web_fetch("https://example.com/big.png")
        assert isinstance(result, str)
        assert "limit" in result
