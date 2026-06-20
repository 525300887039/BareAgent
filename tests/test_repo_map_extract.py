"""Tests for the tree-sitter tag extractor (memory/repo_map_extract.py).

These require the optional ``[repo-map]`` extra (tree-sitter + grammars). When
it is not installed, ``build_extractor`` returns ``None`` and the whole module is
skipped -- mirroring the fail-open boot gate (no extra -> no tool).
"""

from __future__ import annotations

import pytest

from bareagent.memory.repo_map_extract import build_extractor

pytestmark = pytest.mark.skipif(
    build_extractor() is None,
    reason="repo-map extra (tree-sitter + grammars) not installed",
)


def _kinds_names(file_tags):
    return [(d.kind, d.name) for d in file_tags.definitions]


def test_extract_python_definitions_and_references():
    extractor = build_extractor()
    src = (
        "class Foo:\n"
        "    def bar(self, x: int) -> str:\n"
        "        return helper(str(x))\n"
        "\n"
        "def baz(a, b=0):\n"
        "    return a + b\n"
    )
    tags = extractor.extract("m.py", src)
    assert tags is not None
    assert ("class", "Foo") in _kinds_names(tags)
    assert ("function", "bar") in _kinds_names(tags)
    assert ("function", "baz") in _kinds_names(tags)
    # references feed the PageRank graph
    assert "helper" in [r.name for r in tags.references]


def test_extract_python_signature_elides_body_and_keeps_params():
    extractor = build_extractor()
    src = "def greet(name: str, polite: bool = True) -> str:\n    return name\n"
    tags = extractor.extract("g.py", src)
    sig = next(d.signature for d in tags.definitions if d.name == "greet")
    assert sig == "def greet(name: str, polite: bool = True) -> str:"
    # the body is not present
    assert "return name" not in sig


def test_extract_python_line_numbers_are_1_based():
    extractor = build_extractor()
    src = "\n\nclass Widget:\n    pass\n"
    tags = extractor.extract("w.py", src)
    widget = next(d for d in tags.definitions if d.name == "Widget")
    assert widget.start_line == 3


def test_extract_unsupported_extension_returns_none():
    extractor = build_extractor()
    assert extractor.extract("notes.md", "# hello\n") is None
    assert extractor.extract("data.json", "{}\n") is None


def test_extract_routes_by_extension():
    extractor = build_extractor()
    # JavaScript routes to the JS grammar and finds the class + method.
    js = "class Foo {\n  bar(x) { return helper(x); }\n}\n"
    tags = extractor.extract("a.js", js)
    if tags is None:  # grammar for this language may be unavailable
        pytest.skip("javascript grammar not installed")
    names = [d.name for d in tags.definitions]
    assert "Foo" in names
    assert "bar" in names


def test_extractor_identity_is_stable():
    a = build_extractor().identity
    b = build_extractor().identity
    assert a == b
    assert a.startswith("treesitter:")
