from __future__ import annotations

from unittest.mock import patch

from src.core.handlers.web_search import _format_results, run_web_search


class TestFormatResults:
    def test_empty_results(self):
        result = _format_results([], "test query")
        assert "No results found" in result
        assert "test query" in result

    def test_single_result(self):
        results = [
            {"title": "Example", "url": "https://example.com", "snippet": "A snippet"}
        ]
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


class TestRunWebSearch:
    def test_empty_query(self):
        result = run_web_search("")
        assert "Error" in result
        assert "empty" in result

    def test_whitespace_query(self):
        result = run_web_search("   ")
        assert "Error" in result

    @patch("src.core.handlers.web_search._search_ddg_html")
    def test_ddg_fallback_used_when_no_brave_key(self, mock_ddg):
        mock_ddg.return_value = [
            {"title": "Test", "url": "https://test.com", "snippet": "test snippet"}
        ]
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            result = run_web_search("python")
        assert "Test" in result
        mock_ddg.assert_called_once()

    @patch("src.core.handlers.web_search._search_brave")
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

    @patch("src.core.handlers.web_search._search_ddg_html")
    def test_network_error_returns_message(self, mock_ddg):
        from urllib.error import URLError

        mock_ddg.side_effect = URLError("Connection refused")
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            result = run_web_search("test")
        assert "Error" in result
