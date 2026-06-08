"""Unit tests for the semantic-recall embedding layer (src/memory/embedding.py)."""

from __future__ import annotations

from pathlib import Path

from src.memory.embedding import (
    EmbeddingCache,
    build_embedder,
    cosine,
    text_hash,
)

# -- cosine ----------------------------------------------------------------


def test_cosine_identical_vectors_is_one():
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_degenerate_inputs_are_zero():
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero-norm
    assert cosine([1.0, 2.0], [1.0]) == 0.0  # length mismatch


def test_cosine_ranks_closer_vector_higher():
    query = [1.0, 1.0, 0.0]
    near = [1.0, 0.9, 0.1]
    far = [0.0, 0.1, 1.0]
    assert cosine(query, near) > cosine(query, far)


# -- text_hash -------------------------------------------------------------


def test_text_hash_stable_and_distinct():
    assert text_hash("hello") == text_hash("hello")
    assert text_hash("hello") != text_hash("world")


# -- EmbeddingCache --------------------------------------------------------


def test_embedding_cache_round_trip(tmp_path: Path):
    path = tmp_path / "cache.json"
    cache = EmbeddingCache(path, identity="openai:m")
    cache.put("a.md", "h1", [0.1, 0.2])
    cache.save()

    reloaded = EmbeddingCache(path, identity="openai:m")
    assert reloaded.get("a.md") == ("h1", [0.1, 0.2])


def test_embedding_cache_invalidated_on_identity_change(tmp_path: Path):
    path = tmp_path / "cache.json"
    cache = EmbeddingCache(path, identity="openai:m1")
    cache.put("a.md", "h1", [0.1])
    cache.save()

    # A different backend/model identity starts fresh (stale vectors ignored).
    other = EmbeddingCache(path, identity="local:m2")
    assert other.get("a.md") is None


def test_embedding_cache_corrupt_file_starts_fresh(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text("not json {{{", encoding="utf-8")
    cache = EmbeddingCache(path, identity="openai:m")
    assert cache.get("a.md") is None  # did not raise


def test_embedding_cache_prune_drops_missing(tmp_path: Path):
    cache = EmbeddingCache(tmp_path / "cache.json", identity="openai:m")
    cache.put("a.md", "h", [0.1])
    cache.put("b.md", "h", [0.2])
    cache.prune({"a.md"})
    assert cache.get("a.md") is not None
    assert cache.get("b.md") is None


# -- build_embedder (fail-open) --------------------------------------------


def test_build_embedder_unknown_backend_returns_none():
    assert build_embedder("nonsense", "model") is None


def test_build_embedder_local_without_fastembed_returns_none():
    # fastembed is an optional extra, absent in the dev env -> fail-open to None.
    assert build_embedder("local", "BAAI/bge-small-en-v1.5") is None
