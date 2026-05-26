from __future__ import annotations

import json
import os
import re
from urllib.error import URLError
from urllib.parse import quote_plus, unquote
from urllib.request import Request, urlopen

from src.core.handlers.web_fetch import _DEFAULT_TIMEOUT, _USER_AGENT, html_to_text

_DEFAULT_MAX_RESULTS = 5
_MAX_READ_BYTES = 512_000

_RE_DDG_LINK = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]*)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_RE_DDG_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_RE_DDG_UDDG = re.compile(r"uddg=([^&]+)")


def _search_brave(
    query: str,
    max_results: int,
    timeout: int,
    api_key: str,
) -> list[dict[str, str]]:
    """通过 Brave Search API 搜索。"""
    url = (
        f"https://api.search.brave.com/res/v1/web/search"
        f"?q={quote_plus(query)}&count={max_results}"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Subscription-Token": api_key,
        },
    )
    with urlopen(request, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read(_MAX_READ_BYTES).decode("utf-8"))

    results: list[dict[str, str]] = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            }
        )
    return results


def _search_ddg_html(
    query: str,
    max_results: int,
    timeout: int,
) -> list[dict[str, str]]:
    """通过 DuckDuckGo HTML 页面抓取搜索结果（零配置回退）。"""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as resp:  # noqa: S310
        body = resp.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")

    results: list[dict[str, str]] = []
    links = _RE_DDG_LINK.findall(body)
    snippets = _RE_DDG_SNIPPET.findall(body)

    for i, (href, title_html) in enumerate(links[:max_results]):
        title = html_to_text(title_html).strip()
        snippet = ""
        if i < len(snippets):
            snippet = html_to_text(snippets[i]).strip()
        actual_url = href
        if "uddg=" in href:
            match = _RE_DDG_UDDG.search(href)
            if match:
                actual_url = unquote(match.group(1))
        results.append(
            {
                "title": title,
                "url": actual_url,
                "snippet": snippet,
            }
        )
    return results


def _format_results(results: list[dict[str, str]], query: str) -> str:
    """将搜索结果格式化为可读文本。"""
    if not results:
        return f"No results found for: {query}"

    lines: list[str] = [f"Search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def run_web_search(
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Search the web and return formatted results."""
    if not query.strip():
        return "Error: search query cannot be empty."

    brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()

    try:
        if brave_api_key:
            results = _search_brave(query, max_results, timeout, brave_api_key)
        else:
            results = _search_ddg_html(query, max_results, timeout)
    except (URLError, OSError, TimeoutError) as exc:
        return f"Error searching: {exc}"
    except (json.JSONDecodeError, KeyError) as exc:
        return f"Error parsing search results: {exc}"

    return _format_results(results, query)
