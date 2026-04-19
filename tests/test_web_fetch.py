from __future__ import annotations

from src.core.handlers.web_fetch import _truncate, html_to_text, run_web_fetch


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
