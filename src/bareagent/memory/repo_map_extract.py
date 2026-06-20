"""tree-sitter tag extraction for the repo map (the optional ``[repo-map]`` extra).

This is the language-specific half of the repo map: it parses a source file with
tree-sitter and runs a small tag query to pull out class / function / method
*definitions* (with their signature line, sliced via the parser's own ``body``
field) and *call references* (which feed the PageRank reference graph in
:mod:`bareagent.memory.repo_map`). It implements the
:class:`~bareagent.memory.repo_map.TagExtractor` protocol so the pure core can
stay free of any tree-sitter dependency.

Packaging: tree-sitter and the per-language grammars live in the optional
``[repo-map]`` extra. The tag queries are our own (small, owned) ``.scm`` files
under ``repo_map_queries/`` -- adding a language is dropping a ``<lang>.scm`` file
and an extension mapping. Everything is fail-open: a missing extra, a missing
grammar, an unsupported extension, or a parse failure all degrade to ``None`` /
no tags rather than raising, so :func:`build_extractor` returns ``None`` and the
boot wiring simply withholds the ``repo_map`` tool (no dead tool exposed).
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

from bareagent.memory.repo_map import Definition, FileTags, Reference, TagExtractor

logger = logging.getLogger(__name__)

# Language name -> grammar distribution module (each in the [repo-map] extra).
_LANGUAGE_MODULES = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "rust": "tree_sitter_rust",
    "go": "tree_sitter_go",
    "java": "tree_sitter_java",
}

# File extension -> language name. TypeScript (.ts/.tsx) is intentionally absent
# (out of scope: TS grammar nodes differ from JS, no query shipped yet).
_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
}


def _load_query_text(language: str) -> str | None:
    """Read a vendored ``<language>.scm`` tag query, or ``None`` if absent."""
    try:
        resource = files("bareagent.memory") / "repo_map_queries" / f"{language}.scm"
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return None


@dataclass(slots=True)
class _LanguageBundle:
    parser: Any  # tree_sitter.Parser (untyped SDK boundary)
    query: Any  # tree_sitter.Query (untyped SDK boundary)


class TreeSitterExtractor:
    """Extracts :class:`FileTags` via tree-sitter, lazily per language.

    Grammar + query objects are built on first use of a language and cached;
    languages whose grammar or query is unavailable cache ``None`` so they are
    not retried. ``identity`` folds the tree-sitter version and a hash of all
    loaded query texts so the on-disk tag cache invalidates when either changes.
    """

    def __init__(self, ts_version: str, query_texts: dict[str, str]) -> None:
        self._ts_version = ts_version
        self._query_texts = query_texts
        self._bundles: dict[str, _LanguageBundle | None] = {}
        digest = hashlib.sha256(
            "\x00".join(f"{k}={v}" for k, v in sorted(query_texts.items())).encode("utf-8")
        ).hexdigest()[:12]
        self.identity = f"treesitter:{ts_version}:{digest}"

    @staticmethod
    def _language_for(relpath: str) -> str | None:
        suffix = PurePosixPath(relpath).suffix.lower()
        return _EXTENSION_LANGUAGES.get(suffix)

    def _bundle(self, language: str) -> _LanguageBundle | None:
        if language in self._bundles:
            return self._bundles[language]
        bundle = self._build_bundle(language)
        self._bundles[language] = bundle
        return bundle

    def _build_bundle(self, language: str) -> _LanguageBundle | None:
        module_name = _LANGUAGE_MODULES.get(language)
        scm = self._query_texts.get(language)
        if module_name is None or scm is None:
            return None
        try:
            from tree_sitter import Language, Parser, Query

            grammar = importlib.import_module(module_name)
            lang = Language(grammar.language())
            parser = Parser(lang)
            query = Query(lang, scm)
            return _LanguageBundle(parser=parser, query=query)
        except Exception:
            logger.debug("repo_map: grammar for %r unavailable", language, exc_info=True)
            return None

    def extract(self, relpath: str, source: str) -> FileTags | None:
        language = self._language_for(relpath)
        if language is None:
            return None
        bundle = self._bundle(language)
        if bundle is None:
            return None
        try:
            return self._extract(relpath, source, bundle)
        except Exception:
            logger.debug("repo_map: extraction failed for %s", relpath, exc_info=True)
            return None

    def _extract(self, relpath: str, source: str, bundle: _LanguageBundle) -> FileTags:
        from tree_sitter import QueryCursor

        source_bytes = source.encode("utf-8")
        tree = bundle.parser.parse(source_bytes)
        cursor = QueryCursor(bundle.query)
        defs: list[Definition] = []
        refs: list[Reference] = []
        for _pattern, captures in cursor.matches(tree.root_node):
            def_cap = next((c for c in captures if c.startswith("definition.")), None)
            name_nodes = captures.get("name")
            name = name_nodes[0].text.decode("utf-8", "replace") if name_nodes else ""
            if def_cap:
                node = captures[def_cap][0]
                defs.append(
                    Definition(
                        name=name,
                        kind=def_cap.split(".", 1)[1],
                        signature=_slice_signature(source_bytes, node),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
            elif any(c.startswith("reference") for c in captures) and name:
                refs.append(Reference(name=name))
        return FileTags(relpath=relpath, definitions=tuple(defs), references=tuple(refs))


def _slice_signature(source_bytes: bytes, node: Any) -> str:
    """Signature = declaration text before the body (body elided).

    Uses the parser's own ``body`` field so the cut is language-agnostic; when a
    definition has no body field (e.g. a Rust struct), the first line is used.
    ``node`` is an untyped tree-sitter ``Node`` (SDK boundary, hence ``Any``).
    """
    start = node.start_byte
    body = node.child_by_field_name("body")
    if body is not None and body.start_byte > start:
        raw = source_bytes[start : body.start_byte]
    else:
        raw = source_bytes[start : node.end_byte].split(b"\n", 1)[0]
    return raw.decode("utf-8", "replace").strip()


def build_extractor() -> TagExtractor | None:
    """Construct the tree-sitter extractor, or ``None`` when unusable (fail-open).

    Returns ``None`` if tree-sitter is not installed (the ``[repo-map]`` extra is
    absent) or no language bundle can be built, so the caller withholds the tool
    rather than exposing a dead one.
    """
    try:
        import tree_sitter  # noqa: F401
    except ImportError:
        return None

    try:
        from importlib.metadata import version

        ts_version = version("tree-sitter")
    except Exception:
        ts_version = "0"

    query_texts: dict[str, str] = {}
    for language in _LANGUAGE_MODULES:
        text = _load_query_text(language)
        if text is not None:
            query_texts[language] = text
    if not query_texts:
        return None

    extractor = TreeSitterExtractor(ts_version, query_texts)
    # Verify at least one language actually builds; otherwise the tool is useless.
    if not any(extractor._bundle(lang) is not None for lang in query_texts):
        return None
    return extractor
