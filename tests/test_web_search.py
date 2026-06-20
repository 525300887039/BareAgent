from __future__ import annotations

import base64
from unittest.mock import patch

from bareagent.core.handlers.web_search import (
    _decode_bing_url,
    _format_results,
    _parse_bing_html,
    run_web_search,
)


def _bing_block(title_html: str, real_url: str, snippet: str) -> str:
    """Build a Bing <li class="b_algo"> block whose ck/a href encodes `real_url`."""
    encoded = base64.urlsafe_b64encode(real_url.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&amp;&amp;u=a1{encoded}&amp;ntb=1"
    return (
        '<li class="b_algo"><h2 class="">'
        f'<a target="_blank" href="{href}">{title_html}</a></h2>'
        f'<div class="b_caption"><p class="b_lineclamp2">{snippet}</p></div></li>'
    )


class TestFormatResults:
    def test_empty_results(self):
        result = _format_results([], "test query")
        assert "No results found" in result
        assert "test query" in result

    def test_single_result(self):
        results = [{"title": "Example", "url": "https://example.com", "snippet": "A snippet"}]
        text = _format_results(results, "test")
        assert "1. Example" in text
        assert "https://example.com" in text
        assert "A snippet" in text

    def test_multiple_results(self):
        results = [
            {
                "title": f"Result {i}",
                "url": f"https://example.com/{i}",
                "snippet": f"Snippet {i}",
            }
            for i in range(3)
        ]
        text = _format_results(results, "test")
        assert "1. Result 0" in text
        assert "2. Result 1" in text
        assert "3. Result 2" in text


class TestDecodeBingUrl:
    def test_decodes_ck_redirect_to_real_url(self):
        real = "https://www.python.org/"
        encoded = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
        href = f"https://www.bing.com/ck/a?!&amp;&amp;u=a1{encoded}&amp;ntb=1"
        assert _decode_bing_url(href) == real

    def test_passthrough_when_no_marker(self):
        href = "https://direct.example.com/page"
        assert _decode_bing_url(href) == href


class TestParseBingHtml:
    def test_extracts_title_url_snippet(self):
        body = (
            '<ol id="b_results">'
            + _bing_block(
                "Welcome to <strong>Python</strong>.org",
                "https://www.python.org/",
                "The official home of the <strong>Python</strong> language.",
            )
            + _bing_block(
                "Python docs",
                "https://docs.python.org/",
                "Documentation snippet.",
            )
            + "</ol>"
        )
        results = _parse_bing_html(body)
        assert len(results) == 2
        # <strong> stripped via html_to_text
        assert results[0]["title"] == "Welcome to Python.org"
        # ck/a redirect decoded to the real URL
        assert results[0]["url"] == "https://www.python.org/"
        assert "official home" in results[0]["snippet"]
        assert results[1]["url"] == "https://docs.python.org/"

    def test_returns_empty_for_unparseable_body(self):
        assert _parse_bing_html("<html><body>no results here</body></html>") == []


class TestRunWebSearch:
    def test_empty_query(self):
        result = run_web_search("")
        assert "Error" in result
        assert "empty" in result

    def test_whitespace_query(self):
        result = run_web_search("   ")
        assert "Error" in result

    @patch("bareagent.core.handlers.web_search._search_bing_html")
    def test_bing_used_when_no_brave_key(self, mock_bing):
        mock_bing.return_value = [
            {"title": "Test", "url": "https://test.com", "snippet": "test snippet"}
        ]
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            result = run_web_search("python")
        assert "Test" in result
        mock_bing.assert_called_once()

    @patch("bareagent.core.handlers.web_search._search_brave")
    def test_brave_used_when_key_present(self, mock_brave):
        mock_brave.return_value = [
            {
                "title": "Brave Result",
                "url": "https://brave.com",
                "snippet": "brave snippet",
            }
        ]
        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "test-key"}):
            result = run_web_search("python")
        assert "Brave Result" in result
        mock_brave.assert_called_once()

    @patch("bareagent.core.handlers.web_search._search_bing_html")
    def test_network_error_returns_message(self, mock_bing):
        from urllib.error import URLError

        mock_bing.side_effect = URLError("Connection refused")
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            result = run_web_search("test")
        assert "Error" in result

    @patch("bareagent.core.handlers.web_search._search_bing_html")
    def test_anti_bot_page_surfaces_explicit_error(self, mock_bing):
        # An anti-bot / unsupported-browser page must not be reported as "No results".
        mock_bing.side_effect = RuntimeError(
            "Bing returned no parseable results (likely an anti-bot or unsupported-browser page)."
        )
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            result = run_web_search("test")
        assert "Error" in result
        assert "anti-bot" in result
        assert "No results found" not in result
