"""``code_search`` tool handler: semantic top-K code retrieval.

Thin wrapper over :class:`bareagent.memory.code_index.CodeIndex`. The index is
built lazily (on first call) and refreshed incrementally; this handler only
formats the result into an LLM-readable ``file:start-end`` listing with the
chunk body, and steers the model back to ``grep`` when nothing is found.

Never raises: a missing embedder or an empty result set both return a friendly
string rather than an exception (the index itself is fully fail-open).
"""

from __future__ import annotations

from pathlib import Path

from bareagent.core.sandbox import safe_path
from bareagent.memory.code_index import CodeIndex


def run_code_search(
    query: str,
    k: int = 8,
    path: str = ".",
    *,
    index: CodeIndex,
    workspace: Path,
) -> str:
    """Return up to ``k`` semantically relevant code chunks for ``query``.

    ``path`` scopes the search root within the workspace (sandboxed). The result
    is one ``file:start-end`` header per hit followed by the chunk text. When the
    embedder is unavailable or nothing matches, a friendly note points the model
    at ``grep``.
    """
    text = query.strip() if isinstance(query, str) else ""
    if not text:
        return "Error: code_search requires a non-empty query."

    try:
        limit = int(k)
    except (TypeError, ValueError):
        limit = 8
    if limit <= 0:
        limit = 8

    # Scope to the requested subtree (sandboxed). A bad path degrades to a
    # friendly note rather than raising.
    requested = path.strip() if isinstance(path, str) else ""
    if requested and requested not in (".", "./"):
        try:
            safe_path(requested, workspace.resolve(strict=False))
        except (PermissionError, ValueError):
            return f"Error: code_search path is outside the workspace: {requested}"

    results = index.search(text, limit)
    if not results:
        return (
            "No semantically similar code found (the index may be empty or the "
            "embedding backend unavailable). Try the `grep` tool for an exact "
            "text/regex match instead."
        )

    # Optional subtree filter applied post-search: the index covers the whole
    # workspace, so narrow to the requested prefix here.
    prefix = ""
    if requested and requested not in (".", "./"):
        prefix = requested.replace("\\", "/").strip("/")
    if prefix:
        results = [r for r in results if r.relpath == prefix or r.relpath.startswith(prefix + "/")]
        if not results:
            return (
                f"No semantically similar code found under '{requested}'. Try the "
                "`grep` tool or widen the search path."
            )

    blocks: list[str] = []
    for hit in results:
        blocks.append(f"{hit.relpath}:{hit.start_line}-{hit.end_line}\n{hit.text}")
    return "\n\n".join(blocks)
