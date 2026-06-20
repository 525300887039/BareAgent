"""Repo map: whole-repository symbol skeleton ranked by PageRank.

This module is the engine behind the ``repo_map`` tool. It gives the LLM a
structural panorama of a codebase -- class / function *signature* skeletons
(declaration lines, bodies elided) grouped by file and ordered by importance --
without reading whole files or running repeated greps. It is the sister feature
to semantic code search (:mod:`bareagent.memory.code_index`): ``code_search``
finds *relevant chunks*, ``repo_map`` gives *structure*.

Design (mirroring Aider's repo map, adapted to BareAgent's on-demand tool model):

* **tree-sitter symbols, injected extractor.** The language-specific work --
  parsing a file and slicing out signature lines -- lives behind the
  :class:`TagExtractor` protocol. This module never imports tree-sitter; the
  real extractor (:mod:`bareagent.memory.repo_map_extract`) is injected, so the
  graph / PageRank / render / budget logic here is unit-testable with synthetic
  tags and no optional dependency.
* **PageRank ranking with personalization.** Files are graph nodes; a reference
  to an identifier defined in another file is an edge (weighted by mention count
  and identifier rarity). A hand-rolled power iteration (no networkx) ranks the
  files; a personalization vector biases the walk toward "focus" files (recently
  read / edited, or explicitly passed), so the map foregrounds what the caller
  cares about.
* **Token-budget binary search.** Files are rendered in rank order and the
  largest prefix that fits ``max_tokens`` (approximate, chars/4 -- no tiktoken)
  is emitted, so a large repo still produces a bounded map.
* **Lazy, cached, fail-open.** Per-file extraction is cached by content hash
  (mirroring :class:`~bareagent.memory.embedding.EmbeddingCache`): only changed
  / new files are re-parsed, deleted files are pruned. Any failure degrades to
  an empty map rather than a crash -- the map is an enhancement, not a hard
  dependency.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from bareagent.core.handlers.search_utils import iter_search_files
from bareagent.memory.embedding import text_hash

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 1024
DEFAULT_MAX_FILE_BYTES = 1_048_576  # 1 MB, mirroring grep / code_index policy
DEFAULT_DAMPING = 0.85
DEFAULT_MAX_ITER = 100
DEFAULT_TOLERANCE = 1.0e-6
_CHARS_PER_TOKEN = 4  # crude token approximation; avoids a tiktoken dependency


@dataclass(frozen=True, slots=True)
class Definition:
    """A symbol declaration: its signature line(s), kind, and full node range.

    ``start_line`` / ``end_line`` are 1-based inclusive and span the *whole*
    definition node (body included) so containment between definitions can be
    computed for nesting. ``signature`` is the declaration text only (body
    elided), already sliced by the extractor.
    """

    name: str
    kind: str
    signature: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class Reference:
    """An identifier referenced (called / used) somewhere in a file."""

    name: str


@dataclass(frozen=True, slots=True)
class FileTags:
    """The extracted symbol tags for one source file (the cached unit)."""

    relpath: str
    definitions: tuple[Definition, ...] = ()
    references: tuple[Reference, ...] = ()


@runtime_checkable
class TagExtractor(Protocol):
    """Extracts :class:`FileTags` from a file's source via tree-sitter.

    ``identity`` is a stable string (tree-sitter version + query-set version)
    used to invalidate the on-disk tag cache wholesale when either changes.
    :meth:`extract` returns ``None`` when the file's language is unsupported or
    parsing fails -- the caller treats that as "no tags" (fail-open per file).
    """

    identity: str

    def extract(self, relpath: str, source: str) -> FileTags | None: ...


# ---------------------------------------------------------------------------
# Token approximation
# ---------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """Approximate token count as ``ceil(len/4)`` (no tiktoken dependency)."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Graph construction + PageRank (pure, no dependency)
# ---------------------------------------------------------------------------


def build_reference_graph(
    files: Sequence[FileTags],
) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Build a file-level reference graph from extracted tags.

    Nodes are file relpaths. An edge ``referencer -> definer`` is added when a
    file references an identifier defined in another file, weighted by mention
    count times identifier rarity (``1 / number_of_definers`` -- a TF-IDF-style
    signal where identifiers defined in many files carry less weight). Returns
    ``(nodes, out_edges)`` where ``out_edges[src][dst]`` is the summed weight.
    """
    nodes = [ft.relpath for ft in files]
    # identifier name -> set of files defining it
    definers: dict[str, set[str]] = {}
    for ft in files:
        for d in ft.definitions:
            if d.name:
                definers.setdefault(d.name, set()).add(ft.relpath)

    out_edges: dict[str, dict[str, float]] = {}
    for ft in files:
        # count references per identifier within this file
        ref_counts: dict[str, int] = {}
        for ref in ft.references:
            if ref.name:
                ref_counts[ref.name] = ref_counts.get(ref.name, 0) + 1
        for name, count in ref_counts.items():
            targets = definers.get(name)
            if not targets:
                continue
            # do not reward a file for referencing its own definitions
            external = [t for t in targets if t != ft.relpath]
            if not external:
                continue
            rarity = 1.0 / len(targets)
            weight = count * rarity
            dst_map = out_edges.setdefault(ft.relpath, {})
            for target in external:
                dst_map[target] = dst_map.get(target, 0.0) + weight
    return nodes, out_edges


def pagerank(
    nodes: Sequence[str],
    out_edges: Mapping[str, Mapping[str, float]],
    *,
    personalization: Mapping[str, float] | None = None,
    damping: float = DEFAULT_DAMPING,
    max_iter: int = DEFAULT_MAX_ITER,
    tol: float = DEFAULT_TOLERANCE,
) -> dict[str, float]:
    """Hand-rolled PageRank via power iteration (no networkx).

    ``personalization`` biases the random-walk teleport distribution toward the
    given nodes (focus files); when absent or all-zero the teleport is uniform
    (standard PageRank). Dangling nodes (no out-edges) redistribute their mass
    according to the teleport distribution. Deterministic for a given input.
    """
    node_list = list(nodes)
    n = len(node_list)
    if n == 0:
        return {}
    node_set = set(node_list)

    # teleport / personalization distribution
    if personalization:
        total_p = sum(max(0.0, personalization.get(x, 0.0)) for x in node_list)
    else:
        total_p = 0.0
    if total_p > 0:
        teleport = {x: max(0.0, personalization.get(x, 0.0)) / total_p for x in node_list}
    else:
        teleport = {x: 1.0 / n for x in node_list}

    # normalize out-edge weights per source (drop non-positive weights)
    norm: dict[str, dict[str, float]] = {}
    for src in node_list:
        edges = out_edges.get(src)
        if not edges:
            continue
        positive = {d: w for d, w in edges.items() if w > 0 and d in node_set}
        total = sum(positive.values())
        if total > 0:
            norm[src] = {d: w / total for d, w in positive.items()}

    rank = {x: 1.0 / n for x in node_list}
    for _ in range(max_iter):
        new = {x: (1.0 - damping) * teleport[x] for x in node_list}
        dangling_mass = sum(rank[x] for x in node_list if x not in norm)
        if dangling_mass:
            for x in node_list:
                new[x] += damping * dangling_mass * teleport[x]
        for src, edges in norm.items():
            contribution = damping * rank[src]
            for dst, w in edges.items():
                new[dst] += contribution * w
        diff = sum(abs(new[x] - rank[x]) for x in node_list)
        rank = new
        if diff < tol:
            break
    return rank


def resolve_focus(
    focus: Iterable[str],
    files: Sequence[FileTags],
) -> dict[str, float]:
    """Turn a focus list (file relpaths and/or identifiers) into node weights.

    A focus entry matching a known file relpath boosts that file directly; an
    entry matching a defined identifier boosts the file(s) defining it. Unknown
    entries are ignored. The returned mapping seeds the PageRank personalization
    vector (empty -> uniform teleport).
    """
    rels = {ft.relpath for ft in files}
    definers: dict[str, set[str]] = {}
    for ft in files:
        for d in ft.definitions:
            if d.name:
                definers.setdefault(d.name, set()).add(ft.relpath)

    weights: dict[str, float] = {}
    for raw in focus:
        if not raw:
            continue
        entry = raw.replace("\\", "/").strip()
        if entry in rels:
            weights[entry] = weights.get(entry, 0.0) + 1.0
            continue
        targets = definers.get(entry)
        if targets:
            for target in targets:
                weights[target] = weights.get(target, 0.0) + 1.0
    return weights


# ---------------------------------------------------------------------------
# Rendering + budget
# ---------------------------------------------------------------------------


def _collapse_signature(signature: str) -> str:
    """Flatten a (possibly multi-line) signature into one compact line."""
    parts = [seg.strip() for seg in signature.splitlines() if seg.strip()]
    return " ".join(parts)


def render_file(file_tags: FileTags) -> str:
    """Render one file's definitions as an indented signature tree with line nos.

    Nesting is derived from line-range containment (a method whose range falls
    inside a class's range is indented under it), so the renderer needs no
    language-specific knowledge. Definitions are ordered by source position.
    """
    defs = sorted(file_tags.definitions, key=lambda d: (d.start_line, d.end_line))
    lines = [file_tags.relpath]
    for d in defs:
        depth = 0
        for other in defs:
            if other is d:
                continue
            if (other.start_line, other.end_line) == (d.start_line, d.end_line):
                continue
            if other.start_line <= d.start_line and d.end_line <= other.end_line:
                depth += 1
        indent = "  " * (depth + 1)
        sig = _collapse_signature(d.signature) or d.name
        lines.append(f"{indent}{sig} (L{d.start_line})")
    return "\n".join(lines)


def _under_prefix(relpath: str, prefix: str) -> bool:
    if not prefix:
        return True
    return relpath == prefix or relpath.startswith(prefix + "/")


def _truncate_to_budget(rendered: str, max_tokens: int) -> str:
    """Keep whole leading lines of *rendered* until the token budget is hit."""
    out: list[str] = []
    for line in rendered.split("\n"):
        candidate = "\n".join([*out, line])
        if out and approx_tokens(candidate) > max_tokens:
            break
        out.append(line)
    return "\n".join(out)


def format_repo_map(
    files_by_rel: Mapping[str, FileTags],
    ranked_rels: Sequence[str],
    *,
    path_prefix: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Render ranked files into a token-budgeted map.

    Files are taken in ``ranked_rels`` order (most important first), filtered to
    the ``path_prefix`` subtree, then a binary search picks the largest prefix
    of files whose rendered size fits ``max_tokens``. When even the single
    top-ranked file exceeds the budget it is included and line-truncated rather
    than returning nothing.
    """
    selected = [r for r in ranked_rels if r in files_by_rel and _under_prefix(r, path_prefix)]
    if not selected:
        return ""
    rendered = [render_file(files_by_rel[r]) for r in selected]
    budget = max(max_tokens, 1)

    def joined(count: int) -> str:
        return "\n\n".join(rendered[:count])

    lo, hi, best = 0, len(rendered), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if approx_tokens(joined(mid)) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return _truncate_to_budget(rendered[0], budget)
    return joined(best)


# ---------------------------------------------------------------------------
# On-disk tag cache (mirrors EmbeddingCache: identity + per-file hash + prune)
# ---------------------------------------------------------------------------


def _definition_to_dict(d: Definition) -> dict:
    return {
        "name": d.name,
        "kind": d.kind,
        "signature": d.signature,
        "start_line": d.start_line,
        "end_line": d.end_line,
    }


def _definition_from_dict(raw: dict) -> Definition:
    return Definition(
        name=str(raw.get("name", "")),
        kind=str(raw.get("kind", "")),
        signature=str(raw.get("signature", "")),
        start_line=int(raw.get("start_line", 0) or 0),
        end_line=int(raw.get("end_line", 0) or 0),
    )


def _file_tags_to_dict(ft: FileTags) -> dict:
    return {
        "definitions": [_definition_to_dict(d) for d in ft.definitions],
        "references": [r.name for r in ft.references],
    }


def _file_tags_from_dict(relpath: str, raw: dict) -> FileTags:
    defs = tuple(
        _definition_from_dict(d) for d in raw.get("definitions", []) if isinstance(d, dict)
    )
    refs = tuple(Reference(name=str(name)) for name in raw.get("references", []) if name)
    return FileTags(relpath=relpath, definitions=defs, references=refs)


@dataclass(slots=True)
class _CacheEntry:
    content_hash: str
    tags: FileTags


class RepoMapCache:
    """On-disk per-file tag cache: ``{relpath: (content_hash, FileTags)}``.

    Mirrors :class:`~bareagent.memory.embedding.EmbeddingCache`: invalidated
    wholesale when ``identity`` (tree-sitter + query-set version) changes,
    per-entry when a file's content hash changes, and corrupt files start fresh.
    """

    def __init__(self, path: Path, identity: str) -> None:
        self.path = path
        self.identity = identity
        self._entries: dict[str, _CacheEntry] = {}
        self._load()

    def _load(self) -> None:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("identity") != self.identity:
            return
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return
        for rel, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            tags_raw = entry.get("tags")
            if not isinstance(tags_raw, dict):
                continue
            try:
                self._entries[str(rel)] = _CacheEntry(
                    content_hash=str(entry.get("hash", "")),
                    tags=_file_tags_from_dict(str(rel), tags_raw),
                )
            except (TypeError, ValueError):
                continue

    def get(self, rel: str) -> _CacheEntry | None:
        return self._entries.get(rel)

    def put(self, rel: str, content_hash: str, tags: FileTags) -> None:
        self._entries[rel] = _CacheEntry(content_hash=content_hash, tags=tags)

    def prune(self, live: set[str]) -> int:
        """Drop cached entries for files no longer present; return count removed."""
        removed = 0
        for rel in list(self._entries):
            if rel not in live:
                del self._entries[rel]
                removed += 1
        return removed

    def save(self) -> None:
        import json

        from bareagent.core.fileutil import atomic_write_text

        payload = {
            "identity": self.identity,
            "entries": {
                rel: {"hash": e.content_hash, "tags": _file_tags_to_dict(e.tags)}
                for rel, e in self._entries.items()
            },
        }
        try:
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False))
        except OSError:
            logger.warning("Could not persist repo-map cache to %s", self.path, exc_info=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RepoMapIndex:
    """Lazy, cached repo map over a workspace, ranked by PageRank.

    Construct with an injected :class:`TagExtractor` (the tree-sitter layer) and
    a cache path. :meth:`generate` walks the workspace, extracts tags
    (incrementally via the cache), builds the reference graph, ranks files with
    PageRank biased toward ``focus``, and renders a token-budgeted map. Every
    failure path is fail-open: it returns an empty string rather than raising.
    """

    workspace: Path
    extractor: TagExtractor
    cache_path: Path
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    _resolved: Path = field(init=False)

    def __post_init__(self) -> None:
        self._resolved = self.workspace.expanduser().resolve()

    def generate(
        self,
        *,
        path: str = ".",
        focus: Sequence[str] = (),
        max_tokens: int | None = None,
    ) -> str:
        """Return a ranked, token-budgeted symbol-skeleton map (or "" on failure)."""
        try:
            return self._generate(path, focus, max_tokens)
        except Exception:
            logger.warning("Repo map generation failed; returning empty map.", exc_info=True)
            return ""

    def _generate(
        self,
        path: str,
        focus: Sequence[str],
        max_tokens: int | None,
    ) -> str:
        files = self._collect_tags()
        if not files:
            return ""
        files_by_rel = {ft.relpath: ft for ft in files}
        nodes, out_edges = build_reference_graph(files)
        personalization = resolve_focus(focus, files)
        scores = pagerank(nodes, out_edges, personalization=personalization)
        # Focused files (recently touched + explicit focus) are foregrounded:
        # they sort first, ordered by PageRank among themselves, then everyone
        # else by PageRank. Personalization additionally lifts focus *neighbors*
        # via graph flow. A hard focus-first ordering (rather than relying on the
        # personalization teleport mass alone) keeps a poorly-connected recent
        # file from being buried under well-referenced hubs.
        focus_set = set(personalization)
        ranked = sorted(nodes, key=lambda r: (r not in focus_set, -scores.get(r, 0.0), r))
        prefix = _normalize_prefix(path)
        budget = max_tokens if max_tokens and max_tokens > 0 else self.max_tokens
        return format_repo_map(files_by_rel, ranked, path_prefix=prefix, max_tokens=budget)

    def _collect_tags(self) -> list[FileTags]:
        """Walk the workspace, extracting (and caching) tags per file."""
        cache = RepoMapCache(self.cache_path, self.extractor.identity)
        files: list[FileTags] = []
        live: set[str] = set()
        changed = False
        for file_path in iter_search_files(self._resolved):
            resolved = file_path.resolve(strict=False)
            if not resolved.is_relative_to(self._resolved):
                continue
            try:
                if resolved.stat().st_size > self.max_file_bytes:
                    continue
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = resolved.relative_to(self._resolved).as_posix()
            live.add(rel)
            digest = text_hash(content)
            cached = cache.get(rel)
            if cached is not None and cached.content_hash == digest:
                tags = cached.tags
            else:
                extracted = None
                try:
                    extracted = self.extractor.extract(rel, content)
                except Exception:
                    logger.debug("Tag extraction failed for %s", rel, exc_info=True)
                    extracted = None
                # Cache unsupported / failed files as empty so they are not
                # re-parsed on every call; they simply contribute no tags.
                tags = extracted if extracted is not None else FileTags(relpath=rel)
                cache.put(rel, digest, tags)
                changed = True
            if tags.definitions or tags.references:
                files.append(tags)
        pruned = cache.prune(live)
        if changed or pruned:
            cache.save()
        return files


def _normalize_prefix(path: str) -> str:
    if not path:
        return ""
    norm = path.replace("\\", "/").strip()
    if norm in (".", "./", ""):
        return ""
    return norm.strip("/")
