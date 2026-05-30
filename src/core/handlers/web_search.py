from __future__ import annotations

import base64
import binascii
import html
import json
import os
import re
from urllib.error import URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from src.core.handlers.web_fetch import _DEFAULT_TIMEOUT, html_to_text

_DEFAULT_MAX_RESULTS = 5
_MAX_READ_BYTES = 512_000

_BING_SEARCH_URL = "https://www.bing.com/search"
# Bing serves server-rendered organic results (<li class="b_algo">) only to lightweight /
# non-JS user agents. A modern desktop UA gets a JS shell whose results are injected
# client-side, so a plain HTTP fetch finds nothing. A text-browser UA forces the SSR path.
_BING_UA = "Lynx/2.8.9rel.1 libwww-FM/2.14"

_RE_BING_BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.DOTALL)
_RE_BING_TITLE = re.compile(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", re.DOTALL)
_RE_BING_HREF = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', re.DOTALL)
_RE_BING_SNIPPET = re.compile(r'<div class="b_caption".*?<p[^>]*>(.*?)</p>', re.DOTALL)
# Bing wraps result links in a /ck/a redirect; the real URL is base64url in `u=a1<...>`.
_RE_BING_REDIRECT_U = re.compile(r"[?&]u=a1([^&]+)")


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


def _decode_bing_url(href: str) -> str:
    """Resolve a Bing /ck/a redirect href to the real destination URL.

    The destination is base64url-encoded in the `u=a1<encoded>` query parameter.
    Falls back to the raw (unescaped) href when the marker is absent or undecodable.
    """
    unescaped = html.unescape(href)
    match = _RE_BING_REDIRECT_U.search(unescaped)
    if not match:
        return unescaped
    encoded = match.group(1)
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return unescaped


def _parse_bing_html(body: str) -> list[dict[str, str]]:
    """Extract organic results from a Bing HTML search results page."""
    results: list[dict[str, str]] = []
    for block in _RE_BING_BLOCK.findall(body):
        title_match = _RE_BING_TITLE.search(block)
        href_match = _RE_BING_HREF.search(block)
        if not title_match or not href_match:
            continue
        title = html_to_text(title_match.group(1)).strip()
        url = _decode_bing_url(href_match.group(1))
        snippet_match = _RE_BING_SNIPPET.search(block)
        snippet = html_to_text(snippet_match.group(1)).strip() if snippet_match else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _search_bing_html(
    query: str,
    max_results: int,
    timeout: int,
) -> list[dict[str, str]]:
    """通过抓取 Bing HTML 结果页搜索（零配置、免 key、国内可直连）。"""
    url = f"{_BING_SEARCH_URL}?q={quote_plus(query)}"
    request = Request(
        url,
        headers={"User-Agent": _BING_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urlopen(request, timeout=timeout) as resp:  # noqa: S310
        body = resp.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")

    results = _parse_bing_html(body)
    if results:
        return results[:max_results]
    # Distinguish a genuinely empty result set from an anti-bot / unsupported-browser page
    # so the caller can surface an explicit error instead of a misleading "No results".
    if "there are no results" in body.lower():
        return []
    raise RuntimeError(
        "Bing returned no parseable results (likely an anti-bot or unsupported-browser "
        "page). Set BRAVE_SEARCH_API_KEY to use a reliable search backend."
    )


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
            results = _search_bing_html(query, max_results, timeout)
    except (URLError, OSError, TimeoutError) as exc:
        return f"Error searching: {exc}"
    except RuntimeError as exc:
        return f"Error: {exc}"
    except (json.JSONDecodeError, KeyError) as exc:
        return f"Error parsing search results: {exc}"

    return _format_results(results, query)
