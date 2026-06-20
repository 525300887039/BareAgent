"""Persistent, file-based agent memory.

A memory store is a private directory of Markdown files plus a ``MEMORY.md``
index. The :class:`MemoryManager` exposes six text-editor-style commands
(``view`` / ``create`` / ``str_replace`` / ``insert`` / ``delete`` /
``rename``) whose contract mirrors the Anthropic memory tool, but it is wired
as an ordinary client tool so every provider (Anthropic / OpenAI / DeepSeek)
can use it. The tool itself is content-agnostic; the *meaning* of memory
(frontmatter classification, the index, the "view before acting" habit) is
carried to the model through :data:`MEMORY_PROTOCOL`, injected into the system
prompt.

All paths handed to the manager are resolved relative to the memory root and
validated through :func:`bareagent.core.sandbox.safe_path`, so the model can never
read or write outside its memory directory.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from bareagent.core.fileutil import atomic_write_text
from bareagent.core.sandbox import safe_path
from bareagent.memory.embedding import Embedder, EmbeddingCache, cosine, text_hash

logger = logging.getLogger(__name__)

_INDEX_FILE = "MEMORY.md"
_EMBED_CACHE_FILE = ".embedding-cache.json"
# Prefixes the model may prepend out of habit (the native Anthropic tool uses
# absolute ``/memories/...`` paths). Strip them so paths resolve cleanly under
# the memory root via ``safe_path``.
_STRIP_PREFIXES = ("/memories/", "memories/", "/memory/", "memory/")


class MemoryType(StrEnum):
    """Frontmatter ``metadata.type`` classification for a memory file."""

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


_TYPE_LIST = ", ".join(member.value for member in MemoryType)

MEMORY_PROTOCOL = (
    "<memory-protocol>\n"
    "You have a persistent, cross-session memory stored as Markdown files in a "
    "private memory directory. Use the `memory` tool to read and maintain it.\n"
    "Commands: view, create, str_replace, insert, delete, rename. Paths are "
    'relative to the memory root (e.g. "MEMORY.md", "user/role.md").\n'
    "Protocol:\n"
    "- Before a non-trivial task, `view` the memory directory and read any "
    "relevant memory files.\n"
    "- Persist durable facts the user will rely on across sessions; skip secrets "
    "and details that only matter to the current conversation.\n"
    f"- Each memory is one .md file with YAML frontmatter: name, description, and "
    f"metadata.type (one of: {_TYPE_LIST}).\n"
    "- Keep MEMORY.md as the index: one line per memory "
    "(`- [title](file.md) — hook`). Update it whenever you create, rename, or "
    "delete a memory.\n"
    "</memory-protocol>"
)


def derive_memory_slug(workspace: Path) -> str:
    """Derive a filesystem-safe slug from a workspace path.

    ``D:\\code\\BareAgent`` -> ``D-code-BareAgent``. Used to give each project
    its own memory directory under the shared global root.
    """
    resolved = str(workspace.expanduser().resolve())
    slug = re.sub(r"[:/\\]+", "-", resolved).strip("-")
    return slug or "default"


def default_memory_root(workspace: Path) -> Path:
    """Per-project memory directory under the user-global BareAgent home."""
    return Path.home() / ".bareagent" / "projects" / derive_memory_slug(workspace) / "memory"


def resolve_memory_root(workspace: Path, configured_dir: str) -> Path:
    """Resolve the effective memory root from config.

    Empty ``configured_dir`` falls back to :func:`default_memory_root`. A
    relative override is taken relative to the workspace; an absolute one is
    used as-is.
    """
    configured = configured_dir.strip()
    if not configured:
        return default_memory_root(workspace)
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract top-level string keys from a memory file's YAML frontmatter.

    This is a deliberately minimal, dependency-free parser (no PyYAML): it only
    understands the shape BareAgent writes — a ``---`` fenced block of
    ``key: value`` lines at the very start of the file. Nested blocks such as
    ``metadata:`` are skipped (only flat top-level scalars are returned).

    Returns ``{}`` for any input that does not start with a frontmatter fence,
    or whose fence is never closed. Frontmatter is best-effort metadata, not a
    contract, so malformed input degrades to empty rather than raising.
    """
    if not text.startswith("---\n"):
        return {}
    rest = text[len("---\n") :]
    end = rest.find("\n---")
    if end == -1:
        return {}
    block = rest[:end]
    result: dict[str, str] = {}
    for line in block.split("\n"):
        # Only flat top-level keys: indented lines belong to nested blocks
        # (e.g. under ``metadata:``) and are ignored.
        if not line or line[0] in (" ", "\t"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


_CJK_RE = re.compile(r"[一-鿿]+")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")


def _lexical_terms(s: str) -> set[str]:
    """Tokenize a string into a bag of lexical terms for relevance scoring.

    ASCII words are lowercased whole tokens; CJK runs are split into sliding
    bigrams (single-character runs are kept as-is) so that Chinese queries match
    without a real word segmenter.
    """
    lowered = s.lower()
    terms: set[str] = set(_ASCII_WORD_RE.findall(lowered))
    for run in _CJK_RE.findall(s):
        if len(run) < 2:
            terms.add(run)
            continue
        for i in range(len(run) - 1):
            terms.add(run[i : i + 2])
    return terms


def _relevance(query: str, text: str) -> int:
    """Number of lexical terms shared between ``query`` and ``text``."""
    return len(_lexical_terms(query) & _lexical_terms(text))


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    """A memory file selected by relevance to a query.

    ``score`` is an integer lexical-overlap count on the lexical path and a
    float cosine similarity on the semantic path; callers only sort on it.
    """

    path: str
    description: str
    score: float


class MemoryManager:
    """Sandboxed file store backing the ``memory`` tool.

    Methods raise stdlib exceptions (``FileNotFoundError``, ``ValueError``,
    ``PermissionError``, ...) on predictable failures; the tool handler
    (:func:`bareagent.core.handlers.memory.run_memory`) translates them into
    ``Error:`` strings for the LLM.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_index_lines: int = 200,
        embedder: Embedder | None = None,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._max_index_lines = max_index_lines
        # Optional semantic-recall backend; None keeps the lexical-only path.
        self._embedder = embedder
        # A single lock serializes read-modify-write commands (str_replace /
        # insert / rename). The agent's memory working set is tiny, so a
        # per-store lock is simpler than per-file and never a bottleneck.
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # -- path handling ----------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Normalize a model-supplied path and confine it to the memory root."""
        normalized = path.strip().replace("\\", "/")
        for prefix in _STRIP_PREFIXES:
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        normalized = normalized.lstrip("/").strip()
        return safe_path(normalized or ".", self._root)

    def _relative(self, resolved: Path) -> str:
        try:
            rel = resolved.relative_to(self._root).as_posix()
        except ValueError:
            rel = resolved.name
        return rel or "."

    # -- commands ---------------------------------------------------------

    def view(self, path: str, view_range: list[int] | None = None) -> str:
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        if resolved.is_dir():
            return self._list_dir(resolved, rel)
        if not resolved.exists():
            raise FileNotFoundError(f"memory path not found: {rel}")
        return self._read_file(resolved, rel, view_range)

    def create(self, path: str, file_text: str) -> str:
        resolved = self._resolve(path)
        if resolved == self._root:
            raise ValueError("cannot create over the memory root")
        with self._lock:
            atomic_write_text(resolved, file_text)
        return f"Created {self._relative(resolved)} ({len(file_text)} chars)"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        if not resolved.is_file():
            raise FileNotFoundError(f"memory file not found: {rel}")
        with self._lock:
            content = resolved.read_text(encoding="utf-8")
            count = content.count(old_str)
            if count == 0:
                raise ValueError(f"old_str not found in {rel}")
            if count > 1:
                raise ValueError(
                    f"old_str is not unique in {rel} (found {count} occurrences); "
                    "add surrounding context to make it unique"
                )
            atomic_write_text(resolved, content.replace(old_str, new_str, 1))
        return f"Edited {rel}"

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        if not resolved.is_file():
            raise FileNotFoundError(f"memory file not found: {rel}")
        with self._lock:
            lines = resolved.read_text(encoding="utf-8").split("\n")
            if insert_line < 0 or insert_line > len(lines):
                raise ValueError(
                    f"insert_line {insert_line} out of range for {rel} (0..{len(lines)})"
                )
            new_lines = insert_text.split("\n")
            updated = lines[:insert_line] + new_lines + lines[insert_line:]
            atomic_write_text(resolved, "\n".join(updated))
        return f"Inserted {len(new_lines)} line(s) into {rel} after line {insert_line}"

    def delete(self, path: str) -> str:
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        if resolved == self._root:
            raise ValueError("cannot delete the memory root")
        if not resolved.exists():
            raise FileNotFoundError(f"memory path not found: {rel}")
        with self._lock:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
        return f"Deleted {rel}"

    def rename(self, old_path: str, new_path: str) -> str:
        source = self._resolve(old_path)
        target = self._resolve(new_path)
        source_rel = self._relative(source)
        target_rel = self._relative(target)
        if not source.exists():
            raise FileNotFoundError(f"memory path not found: {source_rel}")
        if self._root in (source, target):
            raise ValueError("cannot rename the memory root")
        if target.exists():
            raise ValueError(f"destination already exists: {target_rel}")
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        return f"Renamed {source_rel} -> {target_rel}"

    # -- system-prompt injection -----------------------------------------

    def system_prompt_section(self) -> str:
        """Return the ``<memory>`` block injected at session start.

        This is the single seam where memory retrieval is decided: today it
        emits the protocol plus the head of MEMORY.md. A future vector-backed
        store would swap the index head for semantically-selected entries here
        without changing the tool surface.
        """
        index_path = self._root / _INDEX_FILE
        try:
            raw = index_path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            index_block = "\n".join(raw.split("\n")[: self._max_index_lines])
        else:
            index_block = "(no memories saved yet)"
        return (
            "<memory>\n"
            f"{MEMORY_PROTOCOL}\n"
            f'<memory-index file="{_INDEX_FILE}">\n'
            f"{index_block}\n"
            "</memory-index>\n"
            "</memory>"
        )

    # -- recall (lexical retrieval) --------------------------------------

    def recall(self, query: str, k: int = 5) -> list[RecalledMemory]:
        """Return up to ``k`` memories most relevant to ``query``.

        When a semantic ``embedder`` is configured, ranks by embedding cosine
        similarity (so paraphrases match even without shared terms); otherwise,
        or if embedding fails at call time, falls back to lexical term overlap
        (ASCII words + CJK bigrams). The whole store is rescanned per call — the
        working set is tiny. The return shape is identical on both paths.
        """
        if not query.strip():
            return []
        if self._embedder is not None:
            try:
                return self._semantic_recall(query, k)
            except Exception:
                logger.warning(
                    "Semantic recall failed; falling back to lexical recall.",
                    exc_info=True,
                )
        return self._lexical_recall(query, k)

    def _collect_scoring_docs(self) -> list[tuple[str, str, str]]:
        """Scan the store into ``(relpath, scoring_text, description)`` tuples."""
        docs: list[tuple[str, str, str]] = []
        for file in self._root.rglob("*.md"):
            if not file.is_file() or file.name == _INDEX_FILE:
                continue
            try:
                content = file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = parse_frontmatter(content)
            name = meta.get("name", "")
            description = meta.get("description", "")
            if name or description:
                scoring_text = f"{name} {description}".strip()
            else:
                scoring_text = content[:200]
            rel = file.relative_to(self._root).as_posix()
            docs.append((rel, scoring_text, description or name))
        return docs

    def _lexical_recall(self, query: str, k: int) -> list[RecalledMemory]:
        scored: list[RecalledMemory] = []
        for rel, scoring_text, description in self._collect_scoring_docs():
            score = _relevance(query, scoring_text)
            if score <= 0:
                continue
            scored.append(RecalledMemory(path=rel, description=description, score=float(score)))
        # Highest score first; stable secondary sort on path keeps output
        # deterministic when scores tie.
        scored.sort(key=lambda m: (-m.score, m.path))
        return scored[:k]

    def _semantic_recall(self, query: str, k: int) -> list[RecalledMemory]:
        """Embedding cosine ranking. Raises on embed failure (caller falls back)."""
        docs = self._collect_scoring_docs()
        if not docs:
            return []
        cache = EmbeddingCache(self._root / _EMBED_CACHE_FILE, self._embedder.identity)
        # Embed only cache misses / changed files, in one batch.
        pending: list[tuple[str, str]] = []  # (relpath, content_hash)
        pending_texts: list[str] = []
        for rel, scoring_text, _desc in docs:
            digest = text_hash(scoring_text)
            cached = cache.get(rel)
            if cached is None or cached[0] != digest:
                pending.append((rel, digest))
                pending_texts.append(scoring_text)
        if pending_texts:
            vectors = self._embedder.embed(pending_texts)
            for (rel, digest), vector in zip(pending, vectors, strict=True):
                cache.put(rel, digest, vector)
            cache.prune({rel for rel, _t, _d in docs})
            cache.save()
        query_vector = self._embedder.embed([query])[0]
        scored: list[RecalledMemory] = []
        for rel, _scoring_text, description in docs:
            entry = cache.get(rel)
            if entry is None:
                continue
            score = cosine(query_vector, entry[1])
            if score <= 0:
                continue
            scored.append(RecalledMemory(path=rel, description=description, score=score))
        scored.sort(key=lambda m: (-m.score, m.path))
        return scored[:k]

    def recall_section(self, query: str, k: int = 5) -> str:
        """Render :meth:`recall` results as a ``<memory-recall>`` block.

        Returns ``""`` when nothing is relevant, so callers can skip injection.
        """
        hits = self.recall(query, k)
        if not hits:
            return ""
        lines = [
            "<memory-recall>",
            "Memories relevant to the current request — read the file with the "
            "memory tool's view command for full content:",
        ]
        for hit in hits:
            lines.append(f"- {hit.path} — {hit.description}")
        lines.append("</memory-recall>")
        return "\n".join(lines)

    # -- helpers ----------------------------------------------------------

    def _list_dir(self, resolved: Path, rel: str) -> str:
        header = "Memory root:" if rel == "." else f"Directory {rel}:"
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
        if not entries:
            return f"{header}\n(empty)"
        lines = [header]
        for entry in entries:
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"- {entry.name}{suffix}")
        return "\n".join(lines)

    def _read_file(self, resolved: Path, rel: str, view_range: list[int] | None) -> str:
        lines = resolved.read_text(encoding="utf-8").split("\n")
        start, end = 1, len(lines)
        if view_range is not None:
            if len(view_range) != 2:
                raise ValueError("view_range must be [start, end]")
            start, end = int(view_range[0]), int(view_range[1])
            if start < 1 or start > len(lines):
                raise ValueError(
                    f"view_range start {start} out of range for {rel} (1..{len(lines)})"
                )
            if end != -1 and end < start:
                raise ValueError("view_range end must be >= start")
            if end == -1 or end > len(lines):
                end = len(lines)
        numbered = [f"{i}\t{lines[i - 1]}" for i in range(start, end + 1)]
        return "\n".join(numbered)


def build_remember_instruction(text: str) -> str:
    """Build the user-turn instruction for the ``/remember`` command."""
    if text:
        return (
            "Use the `memory` tool to persist the following information to long-term "
            "memory. Distill it into a concise entry, classify it "
            "(user/feedback/project/reference), create the .md file with proper "
            "frontmatter, and update the MEMORY.md index.\n\n"
            f"Information to remember:\n{text}"
        )
    return (
        "Review our recent conversation and use the `memory` tool to persist anything "
        "worth remembering across sessions (user preferences, feedback, project "
        "context, references). Create concise memory files with frontmatter and update "
        "the MEMORY.md index."
    )


def build_forget_instruction(text: str) -> str:
    """Build the user-turn instruction for the ``/forget`` command."""
    if text:
        return (
            "Use the `memory` tool to forget the following. First `view` MEMORY.md and "
            "the memory directory to locate matching entries, then `delete` the matching "
            "memory file(s) and remove their lines from the MEMORY.md index.\n\n"
            f"What to forget:\n{text}"
        )
    return (
        "Use the `memory` tool to review the memory directory (`view` MEMORY.md) and "
        "remove any memories that are now outdated or incorrect, updating the MEMORY.md "
        "index accordingly."
    )
