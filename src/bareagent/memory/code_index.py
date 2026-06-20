"""Semantic code search index: fixed line-window chunking + embedding top-K.

This module is the retrieval engine behind the ``code_search`` tool. It reuses
the embedding layer (:mod:`bareagent.memory.embedding`) verbatim -- the
``Embedder`` protocol, pure-Python ``cosine``, ``text_hash`` content keying, and
the on-disk ``EmbeddingCache`` invalidation pattern -- and adds only what is
specific to *code* search: walking the workspace, splitting files into
fixed-size overlapping line windows, and ranking those chunks against a query.

Design choices (mirroring the semantic-recall path in
:meth:`MemoryManager._semantic_recall`):

* **Fixed line windows, no parser.** Chunks are ``chunk_lines`` lines with
  ``chunk_overlap`` lines of overlap. This is language-agnostic and zero
  dependency; it trades symbol-boundary alignment for applicability. A future
  repo-map task can upgrade to symbol-aware chunking.
* **Lazy, incremental index.** :meth:`CodeIndex.search` builds the index on the
  first call: it embeds only chunks whose content hash is missing or changed,
  prunes chunks for deleted/shrunk files, and writes the cache back. From-never
  -searched users pay nothing.
* **Fail-open everywhere.** A ``None`` embedder, an ``embed`` that raises, or a
  corrupt cache all degrade to "return no results" -- never a crash. Embeddings
  are an enhancement, not a hard dependency (the handler steers the LLM back to
  ``grep`` when search comes up empty).

The module is pure: it has no LLM / loop / REPL / SDK dependency, takes the
``Embedder`` and cache path by injection, and is therefore unit-testable with a
fake embedder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from bareagent.core.handlers.search_utils import iter_search_files
from bareagent.memory.embedding import Embedder, EmbeddingCache, cosine, text_hash

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_LINES = 50
DEFAULT_CHUNK_OVERLAP = 10
DEFAULT_MAX_FILE_BYTES = 1_048_576  # 1 MB, mirroring grep_search.MAX_FILE_SIZE


@dataclass(frozen=True, slots=True)
class CodeChunk:
    """A fixed line-window slice of a source file (1-based inclusive lines)."""

    relpath: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True, slots=True)
class CodeSearchResult:
    """A chunk selected by embedding similarity to a query."""

    relpath: str
    start_line: int
    end_line: int
    text: str
    score: float


def chunk_lines(
    text: str,
    *,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    relpath: str = "",
) -> list[CodeChunk]:
    """Split *text* into fixed overlapping line windows.

    Each window is ``chunk_lines`` lines long and consecutive windows overlap by
    ``chunk_overlap`` lines, so a symbol straddling a boundary still lands whole
    inside at least one window. Line numbers in the returned chunks are 1-based
    and inclusive. Empty / whitespace-only text yields no chunks.

    Defensive on its sizing knobs: a non-positive ``chunk_lines`` falls back to
    the default, and an overlap >= window size is clamped so the window always
    advances (otherwise the loop would never terminate).
    """
    lines = text.split("\n")
    # Drop a single trailing empty string from a final newline so a file that
    # ends in "\n" does not emit a spurious blank last line.
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or not text.strip():
        return []

    window = chunk_lines if chunk_lines > 0 else DEFAULT_CHUNK_LINES
    overlap = chunk_overlap if chunk_overlap >= 0 else DEFAULT_CHUNK_OVERLAP
    # Guarantee forward progress: the step (window - overlap) must be >= 1.
    step = window - overlap
    if step < 1:
        step = window

    chunks: list[CodeChunk] = []
    start = 0
    total = len(lines)
    while start < total:
        end = min(start + window, total)
        segment = lines[start:end]
        if any(line.strip() for line in segment):
            chunks.append(
                CodeChunk(
                    relpath=relpath,
                    start_line=start + 1,
                    end_line=end,
                    text="\n".join(segment),
                )
            )
        if end >= total:
            break
        start += step
    return chunks


def _chunk_key(relpath: str, start_line: int) -> str:
    """Cache key for a chunk: ``relpath#startline`` (start line disambiguates)."""
    return f"{relpath}#{start_line}"


class CodeIndex:
    """Lazy, cached embedding index over a workspace's source files.

    Construct with an injected :class:`~bareagent.memory.embedding.Embedder`
    (``None`` disables semantic search -> :meth:`search` returns ``[]``) and a
    cache file path. The index is built on the first :meth:`search` call and
    refreshed incrementally on every subsequent call.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        embedder: Embedder | None,
        cache_path: Path,
        chunk_lines: int = DEFAULT_CHUNK_LINES,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._embedder = embedder
        self._cache_path = cache_path
        self._chunk_lines = chunk_lines
        self._chunk_overlap = chunk_overlap
        self._max_file_bytes = max_file_bytes

    # -- chunk collection -------------------------------------------------

    def _collect_chunks(self, search_root: Path) -> list[CodeChunk]:
        """Walk *search_root* and split each readable file into line windows.

        Files larger than ``max_file_bytes`` and files that are not valid UTF-8
        are skipped (same policy as ``grep``); ignored trees (.git, __pycache__,
        node_modules, .venv) are excluded by ``iter_search_files``.
        """
        chunks: list[CodeChunk] = []
        for file_path in iter_search_files(search_root):
            resolved = file_path.resolve(strict=False)
            if not resolved.is_relative_to(self._workspace):
                continue
            try:
                if resolved.stat().st_size > self._max_file_bytes:
                    continue
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not content.strip():
                continue
            rel = resolved.relative_to(self._workspace).as_posix()
            chunks.extend(
                chunk_lines(
                    content,
                    chunk_lines=self._chunk_lines,
                    chunk_overlap=self._chunk_overlap,
                    relpath=rel,
                )
            )
        return chunks

    # -- search -----------------------------------------------------------

    def search(self, query: str, k: int = 8) -> list[CodeSearchResult]:
        """Return up to ``k`` code chunks most similar to ``query``.

        Embeds (incrementally, via the cache) every chunk under the workspace,
        embeds the query, and ranks by cosine similarity. ``score <= 0`` chunks
        are dropped (mirrors semantic recall). Returns ``[]`` when no embedder is
        configured, when nothing matches, or -- fail-open -- when embedding fails
        at call time.
        """
        if self._embedder is None:
            return []
        if not query.strip():
            return []
        try:
            return self._search(query, k)
        except Exception:
            logger.warning(
                "Semantic code search failed; returning no results.",
                exc_info=True,
            )
            return []

    def _search(self, query: str, k: int) -> list[CodeSearchResult]:
        """Embedding cosine ranking. Raises on embed failure (caller fails open)."""
        assert self._embedder is not None  # caller (search) guards the None case
        chunks = self._collect_chunks(self._workspace)
        if not chunks:
            return []
        cache = EmbeddingCache(self._cache_path, self._embedder.identity)

        # Embed only cache misses / changed chunks, in one batch. The cache key
        # is relpath#startline; a changed file produces new content hashes for
        # its chunks (and, if it shrank, fewer chunks -> prune removes the tail).
        pending: list[tuple[str, str]] = []  # (chunk_key, content_hash)
        pending_texts: list[str] = []
        live_keys: set[str] = set()
        for chunk in chunks:
            key = _chunk_key(chunk.relpath, chunk.start_line)
            live_keys.add(key)
            digest = text_hash(chunk.text)
            cached = cache.get(key)
            if cached is None or cached[0] != digest:
                pending.append((key, digest))
                pending_texts.append(chunk.text)

        if pending_texts:
            vectors = self._embedder.embed(pending_texts)
            for (key, digest), vector in zip(pending, vectors, strict=True):
                cache.put(key, digest, vector)
        # Prune chunks for deleted files / shrunk tails before persisting.
        cache.prune(live_keys)
        if pending_texts:
            cache.save()

        query_vector = self._embedder.embed([query])[0]
        scored: list[tuple[float, CodeChunk]] = []
        for chunk in chunks:
            entry = cache.get(_chunk_key(chunk.relpath, chunk.start_line))
            if entry is None:
                continue
            score = cosine(query_vector, entry[1])
            if score <= 0:
                continue
            scored.append((score, chunk))

        # Highest score first; stable secondary sort on (relpath, start_line)
        # keeps output deterministic when scores tie.
        scored.sort(key=lambda item: (-item[0], item[1].relpath, item[1].start_line))
        return [
            CodeSearchResult(
                relpath=chunk.relpath,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                score=score,
            )
            for score, chunk in scored[: max(k, 0)]
        ]
