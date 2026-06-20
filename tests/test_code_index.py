"""Unit tests for the semantic code search index (memory/code_index.py).

The index reuses the embedding layer; these tests inject a deterministic fake
embedder so chunking / ranking / cache-increment behavior is verified without a
network or model dependency (mirroring tests/test_memory_recall.py).
"""

from __future__ import annotations

from pathlib import Path

from bareagent.memory.code_index import (
    CodeIndex,
    chunk_lines,
)

# -- fixed line-window chunking --------------------------------------------


def test_chunk_lines_splits_with_overlap():
    text = "\n".join(f"line{i}" for i in range(1, 26))  # 25 lines
    chunks = chunk_lines(text, chunk_lines=10, chunk_overlap=3, relpath="a.py")
    # step = 10 - 3 = 7 -> windows start at lines 1, 8, 15, 22.
    assert [(c.start_line, c.end_line) for c in chunks] == [
        (1, 10),
        (8, 17),
        (15, 24),
        (22, 25),
    ]
    assert all(c.relpath == "a.py" for c in chunks)
    # First chunk holds exactly its 10 source lines.
    assert chunks[0].text == "\n".join(f"line{i}" for i in range(1, 11))


def test_chunk_lines_empty_and_whitespace_yields_nothing():
    assert chunk_lines("", chunk_lines=10, chunk_overlap=2) == []
    assert chunk_lines("\n\n   \n\t\n", chunk_lines=10, chunk_overlap=2) == []


def test_chunk_lines_trailing_newline_no_blank_chunk():
    text = "alpha\nbeta\n"  # trailing newline must not create a 3rd blank line
    chunks = chunk_lines(text, chunk_lines=10, chunk_overlap=2, relpath="x")
    assert len(chunks) == 1
    assert chunks[0].end_line == 2
    assert chunks[0].text == "alpha\nbeta"


def test_chunk_lines_overlap_ge_window_still_advances():
    text = "\n".join(f"l{i}" for i in range(1, 16))  # 15 lines
    # overlap >= window would stall; the module clamps step to the window size.
    chunks = chunk_lines(text, chunk_lines=5, chunk_overlap=10, relpath="y")
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 5), (6, 10), (11, 15)]


# -- fake embedder ---------------------------------------------------------


class _FakeEmbedder:
    """Deterministic embedder keyed on which topic terms a chunk contains.

    Records every ``embed`` batch so tests can assert incremental re-embedding.
    """

    identity = "fake:v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.raise_on_embed = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.raise_on_embed:
            raise RuntimeError("embedding endpoint down")
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text: str) -> list[float]:
        lowered = text.lower()
        auth = 1.0 if "authenticate" in lowered or "login" in lowered else 0.0
        parse = 1.0 if "parse" in lowered or "tokenize" in lowered else 0.0
        return [auth, parse, 0.1]

    @property
    def embedded_count(self) -> int:
        return sum(len(batch) for batch in self.calls)


def _index(workspace: Path, embedder, **kw) -> CodeIndex:
    return CodeIndex(
        workspace,
        embedder=embedder,
        cache_path=workspace / ".code-index.json",
        chunk_lines=kw.get("chunk_lines", 50),
        chunk_overlap=kw.get("chunk_overlap", 10),
        max_file_bytes=kw.get("max_file_bytes", 1_048_576),
    )


# -- retrieval -------------------------------------------------------------


def test_search_returns_topk_by_cosine(tmp_path: Path):
    (tmp_path / "auth.py").write_text(
        "def authenticate(user):\n    return login(user)\n", encoding="utf-8"
    )
    (tmp_path / "parse.py").write_text(
        "def parse(src):\n    return tokenize(src)\n", encoding="utf-8"
    )
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder)

    results = index.search("how do users authenticate and login", k=5)
    assert results, "expected at least one hit"
    assert results[0].relpath == "auth.py"
    assert results[0].score > 0


def test_search_respects_k(tmp_path: Path):
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text(
            f"# file {i}\ndef authenticate_{i}():\n    return login()\n", encoding="utf-8"
        )
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder)
    results = index.search("authenticate login", k=2)
    assert len(results) == 2


def test_search_none_embedder_returns_empty(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    pass\n", encoding="utf-8")
    index = _index(tmp_path, None)
    assert index.search("authenticate", k=5) == []


def test_search_empty_query_returns_empty(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    pass\n", encoding="utf-8")
    index = _index(tmp_path, _FakeEmbedder())
    assert index.search("   ", k=5) == []


def test_search_fail_open_on_embed_error(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    pass\n", encoding="utf-8")
    embedder = _FakeEmbedder()
    embedder.raise_on_embed = True
    index = _index(tmp_path, embedder)
    # Embedder raises -> search must return [] (fail-open), not crash.
    assert index.search("authenticate", k=5) == []


# -- incremental cache -----------------------------------------------------


def test_second_build_only_embeds_changed_chunks(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    return login()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def parse():\n    return tokenize()\n", encoding="utf-8")
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder)

    index.search("authenticate", k=5)
    # First build: 2 chunks + 1 query.
    first_total = embedder.embedded_count
    assert first_total >= 3

    # Modify only a.py; b.py is unchanged and must come from cache.
    (tmp_path / "a.py").write_text(
        "def authenticate():\n    return login(user, password)\n", encoding="utf-8"
    )
    embedder.calls.clear()
    # Fresh CodeIndex so nothing is held in memory -- only the on-disk cache.
    index2 = _index(tmp_path, embedder)
    index2.search("authenticate", k=5)
    # Only the changed a.py chunk re-embeds (+ the query). b.py is cached.
    chunk_texts = [t for batch in embedder.calls for t in batch]
    assert any("password" in t for t in chunk_texts)
    assert all("parse" not in t and "tokenize" not in t for t in chunk_texts)


def test_deleted_file_pruned_from_cache(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    return login()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def parse():\n    return tokenize()\n", encoding="utf-8")
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder)
    index.search("authenticate", k=5)

    (tmp_path / "b.py").unlink()
    embedder.calls.clear()
    index2 = _index(tmp_path, embedder)
    results = index2.search("parse tokenize", k=5)
    # The deleted file must not appear in results.
    assert all(r.relpath != "b.py" for r in results)

    # And its cache entry is pruned: re-reading the cache file shows no b.py key.
    import json

    cache_data = json.loads((tmp_path / ".code-index.json").read_text(encoding="utf-8"))
    assert all(not key.startswith("b.py#") for key in cache_data["entries"])


def test_identity_change_reembeds_everything(tmp_path: Path):
    (tmp_path / "a.py").write_text("def authenticate():\n    return login()\n", encoding="utf-8")
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder)
    index.search("authenticate", k=5)

    # A new embedder identity invalidates the whole on-disk cache.
    new_embedder = _FakeEmbedder()
    new_embedder.identity = "fake:v2"
    index2 = _index(tmp_path, new_embedder)
    new_embedder.calls.clear()
    index2.search("authenticate", k=5)
    # Chunk re-embedded under the new identity (the old vectors were discarded).
    chunk_texts = [t for batch in new_embedder.calls for t in batch]
    assert any("authenticate" in t for t in chunk_texts)


def test_oversized_and_binary_files_skipped(tmp_path: Path):
    # Oversized file: above max_file_bytes -> skipped.
    (tmp_path / "big.py").write_text("authenticate\n" * 1000, encoding="utf-8")
    # Valid small file that should index.
    (tmp_path / "ok.py").write_text("def authenticate():\n    return login()\n", encoding="utf-8")
    # Non-UTF-8 bytes -> read_text raises UnicodeDecodeError -> skipped.
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01authenticate")
    embedder = _FakeEmbedder()
    index = _index(tmp_path, embedder, max_file_bytes=100)
    results = index.search("authenticate login", k=10)
    assert any(r.relpath == "ok.py" for r in results)
    assert all(r.relpath not in ("big.py", "bin.dat") for r in results)
