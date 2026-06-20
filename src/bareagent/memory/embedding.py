"""Pluggable text-embedding backends for semantic memory recall.

This module is the embedding layer behind :meth:`MemoryManager.recall`'s
semantic path. It is deliberately small and dependency-light: the OpenAI
backend reuses the already-present ``openai`` client, the local backend
lazy-imports ``fastembed`` (optional ``[embeddings]`` extra), and cosine
similarity is computed in pure Python so neither backend forces ``numpy`` into
the core install.

Every entry point fails open: :func:`build_embedder` returns ``None`` when the
chosen backend cannot be constructed (missing dependency, missing key, unknown
backend), and the recall layer treats ``None`` -- or any error raised by
``embed`` at call time -- as a signal to fall back to lexical recall. Embeddings
are a relevance enhancement, never a hard dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Protocol, runtime_checkable

from bareagent.core.fileutil import atomic_write_text

logger = logging.getLogger(__name__)

_OPENAI_DEFAULT_MODEL = "text-embedding-3-small"
_LOCAL_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


@runtime_checkable
class Embedder(Protocol):
    """Embeds a batch of strings into float vectors.

    ``identity`` is a stable ``backend:model`` string used to invalidate the
    on-disk vector cache when the backend or model changes.
    """

    identity: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """Embeds via an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        import openai

        self.model = model
        self.identity = f"openai:{model}"
        # max_retries=0: app-layer fail-open owns retry semantics, not the SDK.
        self._client = openai.OpenAI(
            api_key=api_key or "", base_url=base_url or None, max_retries=0
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(model=self.model, input=list(texts))
        return [[float(x) for x in item.embedding] for item in response.data]


class LocalEmbedder:
    """Embeds locally via fastembed (ONNX, no torch; optional ``[embeddings]``)."""

    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self.model = model
        self.identity = f"local:{model}"
        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]


def build_embedder(
    backend: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Embedder | None:
    """Construct the requested embedder, or ``None`` on any failure (fail-open).

    A missing fastembed install, a missing key, or an unknown backend name all
    degrade to ``None`` so the caller falls back to lexical recall.
    """
    normalized = (backend or "").strip().lower()
    try:
        if normalized == "local":
            return LocalEmbedder(model or _LOCAL_DEFAULT_MODEL)
        if normalized == "openai":
            return OpenAIEmbedder(
                model or _OPENAI_DEFAULT_MODEL, base_url=base_url, api_key=api_key
            )
    except Exception:
        logger.warning(
            "Embedding backend %r unavailable; semantic recall will fall back to lexical.",
            normalized,
            exc_info=True,
        )
        return None
    logger.warning("Unknown embedding backend %r; semantic recall disabled.", backend)
    return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python (no numpy). Returns 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def text_hash(text: str) -> str:
    """Stable content hash keying a cached embedding to the text it embedded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """On-disk vector cache: ``{relpath: (text_hash, vector)}`` + backend identity.

    The cache is invalidated wholesale when ``identity`` (backend:model) changes,
    and per-entry when the embedded text's hash changes. Corrupt / unreadable
    cache files start fresh rather than raising.
    """

    def __init__(self, path: Path, identity: str) -> None:
        self.path = path
        self.identity = identity
        self._entries: dict[str, tuple[str, list[float]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("identity") != self.identity:
            return  # backend/model changed or unrecognized layout -> start fresh
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return
        for rel, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            vector = entry.get("vector")
            if not isinstance(vector, list):
                continue
            try:
                self._entries[str(rel)] = (
                    str(entry.get("hash", "")),
                    [float(x) for x in vector],
                )
            except (TypeError, ValueError):
                continue

    def get(self, rel: str) -> tuple[str, list[float]] | None:
        return self._entries.get(rel)

    def put(self, rel: str, content_hash: str, vector: list[float]) -> None:
        self._entries[rel] = (content_hash, vector)

    def prune(self, live: set[str]) -> None:
        for rel in list(self._entries):
            if rel not in live:
                del self._entries[rel]

    def save(self) -> None:
        payload = {
            "identity": self.identity,
            "entries": {rel: {"hash": h, "vector": vec} for rel, (h, vec) in self._entries.items()},
        }
        try:
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False))
        except OSError:
            logger.warning("Could not persist embedding cache to %s", self.path, exc_info=True)
