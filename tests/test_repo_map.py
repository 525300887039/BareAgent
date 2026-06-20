"""Unit tests for the repo map core (memory/repo_map.py).

The core is pure: graph construction, PageRank, focus resolution, rendering, and
the token-budget binary search are exercised with synthetic ``FileTags`` and a
fake :class:`TagExtractor`, so no tree-sitter / optional dependency is needed.
"""

from __future__ import annotations

from pathlib import Path

from bareagent.memory.repo_map import (
    Definition,
    FileTags,
    Reference,
    RepoMapIndex,
    approx_tokens,
    build_reference_graph,
    format_repo_map,
    pagerank,
    render_file,
    resolve_focus,
)


def _defn(name, kind="function", *, start, end, signature=None):
    return Definition(
        name=name,
        kind=kind,
        signature=signature if signature is not None else f"def {name}()",
        start_line=start,
        end_line=end,
    )


# -- graph construction ----------------------------------------------------


def test_build_reference_graph_edges_referencer_to_definer():
    files = [
        FileTags("a.py", definitions=(_defn("foo", start=1, end=5),)),
        FileTags("b.py", references=(Reference("foo"), Reference("foo"))),
    ]
    nodes, edges = build_reference_graph(files)
    assert set(nodes) == {"a.py", "b.py"}
    # b references foo (defined in a) twice; rarity = 1/1 -> weight 2.0
    assert edges["b.py"]["a.py"] == 2.0
    # a has no outgoing references
    assert "a.py" not in edges


def test_build_reference_graph_ignores_self_and_unknown_refs():
    files = [
        FileTags(
            "a.py",
            definitions=(_defn("foo", start=1, end=5),),
            references=(Reference("foo"), Reference("nonexistent")),
        ),
    ]
    _, edges = build_reference_graph(files)
    # only self / unknown references -> no edges
    assert edges == {}


def test_build_reference_graph_rarity_downweights_common_identifiers():
    files = [
        FileTags("a.py", definitions=(_defn("util", start=1, end=2),)),
        FileTags("b.py", definitions=(_defn("util", start=1, end=2),)),
        FileTags("c.py", references=(Reference("util"),)),
    ]
    _, edges = build_reference_graph(files)
    # util defined in 2 files -> rarity 0.5, split as 0.5 to each definer
    assert edges["c.py"]["a.py"] == 0.5
    assert edges["c.py"]["b.py"] == 0.5


# -- PageRank --------------------------------------------------------------


def test_pagerank_uniform_sums_to_one():
    nodes = ["a", "b", "c"]
    edges = {"b": {"a": 1.0}, "c": {"a": 1.0}}
    scores = pagerank(nodes, edges)
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    # a is referenced by both b and c -> highest rank
    assert scores["a"] > scores["b"]
    assert scores["a"] > scores["c"]


def test_pagerank_personalization_biases_focus():
    nodes = ["a", "b"]
    edges: dict[str, dict[str, float]] = {}  # no edges -> all dangling
    biased = pagerank(nodes, edges, personalization={"b": 1.0})
    # with all mass teleporting to b, b dominates
    assert biased["b"] > biased["a"]


def test_pagerank_empty_graph():
    assert pagerank([], {}) == {}


# -- focus resolution ------------------------------------------------------


def test_resolve_focus_matches_files_and_identifiers():
    files = [
        FileTags("pkg/a.py", definitions=(_defn("Widget", "class", start=1, end=9),)),
        FileTags("pkg/b.py"),
    ]
    # a path entry boosts the file; an identifier entry boosts its definer
    weights = resolve_focus(["pkg/b.py", "Widget"], files)
    assert weights["pkg/b.py"] == 1.0
    assert weights["pkg/a.py"] == 1.0


def test_resolve_focus_ignores_unknown_and_normalizes_separators():
    files = [FileTags("pkg/a.py")]
    weights = resolve_focus(["pkg\\a.py", "ghost"], files)
    assert weights == {"pkg/a.py": 1.0}


# -- rendering -------------------------------------------------------------


def test_render_file_nests_by_containment_with_line_numbers():
    files = FileTags(
        "m.py",
        definitions=(
            _defn("Foo", "class", start=1, end=10, signature="class Foo:"),
            _defn("bar", "method", start=2, end=5, signature="def bar(self, x):"),
            _defn("baz", start=12, end=14, signature="def baz():"),
        ),
    )
    out = render_file(files)
    lines = out.split("\n")
    assert lines[0] == "m.py"
    assert lines[1] == "  class Foo: (L1)"
    assert lines[2] == "    def bar(self, x): (L2)"  # nested under Foo
    assert lines[3] == "  def baz(): (L12)"  # top-level again


def test_render_file_collapses_multiline_signature():
    files = FileTags(
        "m.py",
        definitions=(
            _defn("f", start=1, end=3, signature="def f(\n    a,\n    b,\n):"),
        ),
    )
    out = render_file(files)
    assert out.split("\n")[1] == "  def f( a, b, ): (L1)"


# -- budget / formatting ---------------------------------------------------


def test_approx_tokens():
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("abcde") == 2


def test_format_repo_map_path_prefix_filter():
    files_by_rel = {
        "src/a.py": FileTags("src/a.py", definitions=(_defn("a", start=1, end=2),)),
        "tests/b.py": FileTags("tests/b.py", definitions=(_defn("b", start=1, end=2),)),
    }
    out = format_repo_map(
        files_by_rel, ["src/a.py", "tests/b.py"], path_prefix="src", max_tokens=1000
    )
    assert "src/a.py" in out
    assert "tests/b.py" not in out


def test_format_repo_map_budget_drops_lowest_ranked():
    # two sizeable files; a tiny budget keeps only the top-ranked one
    big_sig = "def " + "x" * 200 + "()"
    files_by_rel = {
        "a.py": FileTags("a.py", definitions=(_defn("a", start=1, end=2, signature=big_sig),)),
        "b.py": FileTags("b.py", definitions=(_defn("b", start=1, end=2, signature=big_sig),)),
    }
    out = format_repo_map(files_by_rel, ["a.py", "b.py"], max_tokens=60)
    assert "a.py" in out
    assert "b.py" not in out


def test_format_repo_map_truncates_when_top_file_over_budget():
    big = FileTags(
        "a.py",
        definitions=tuple(
            _defn(f"f{i}", start=i, end=i, signature=f"def f{i}(aaaaaaaaaa)")
            for i in range(1, 30)
        ),
    )
    out = format_repo_map({"a.py": big}, ["a.py"], max_tokens=20)
    assert out.startswith("a.py")
    assert approx_tokens(out) <= 20 + 5  # truncated near budget, never empty
    assert out != ""


# -- index orchestration (fake extractor) ----------------------------------


class _FakeExtractor:
    """Returns predefined tags per relpath and counts extraction calls."""

    identity = "fake:v1"

    def __init__(self, tags_by_rel: dict[str, FileTags]) -> None:
        self._tags = tags_by_rel
        self.calls: list[str] = []

    def extract(self, relpath: str, source: str) -> FileTags | None:
        self.calls.append(relpath)
        return self._tags.get(relpath)


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Workspace subdir + a cache path *outside* it (mirrors production layout)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, tmp_path / "cache.json"


def _write(root: Path, rel: str, content: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_index_generate_ranks_and_renders(tmp_path: Path):
    ws, cache_path = _make_workspace(tmp_path)
    _write(ws, "a.py")
    _write(ws, "b.py")
    extractor = _FakeExtractor(
        {
            "a.py": FileTags("a.py", definitions=(_defn("foo", start=1, end=2),)),
            "b.py": FileTags(
                "b.py", references=(Reference("foo"), Reference("foo"))
            ),
        }
    )
    index = RepoMapIndex(ws, extractor=extractor, cache_path=cache_path)
    out = index.generate(max_tokens=1000)
    # a.py is referenced by b.py -> ranked first
    assert out.index("a.py") < out.index("foo")
    assert "a.py" in out and "b.py" in out


def test_index_incremental_cache_reextracts_only_changed(tmp_path: Path):
    ws, cache_path = _make_workspace(tmp_path)
    _write(ws, "a.py", "v1\n")
    _write(ws, "b.py", "v1\n")
    tags = {
        "a.py": FileTags("a.py", definitions=(_defn("a", start=1, end=1),)),
        "b.py": FileTags("b.py", definitions=(_defn("b", start=1, end=1),)),
    }
    extractor = _FakeExtractor(tags)
    RepoMapIndex(ws, extractor=extractor, cache_path=cache_path).generate()
    assert sorted(extractor.calls) == ["a.py", "b.py"]

    # second run, only b.py changed -> only b.py re-extracted
    extractor.calls.clear()
    _write(ws, "b.py", "v2\n")
    RepoMapIndex(ws, extractor=extractor, cache_path=cache_path).generate()
    assert extractor.calls == ["b.py"]


def test_index_prunes_deleted_files(tmp_path: Path):
    ws, cache_path = _make_workspace(tmp_path)
    _write(ws, "a.py")
    _write(ws, "b.py")
    tags = {
        "a.py": FileTags("a.py", definitions=(_defn("a", start=1, end=1),)),
        "b.py": FileTags("b.py", definitions=(_defn("b", start=1, end=1),)),
    }
    extractor = _FakeExtractor(tags)
    RepoMapIndex(ws, extractor=extractor, cache_path=cache_path).generate()

    (ws / "b.py").unlink()
    import json

    out = RepoMapIndex(ws, extractor=extractor, cache_path=cache_path).generate()
    assert "b.py" not in out
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "b.py" not in cached["entries"]


def test_index_focus_biases_ranking(tmp_path: Path):
    # two unconnected files; focus should float the focused one to the top
    ws, cache_path = _make_workspace(tmp_path)
    _write(ws, "a.py")
    _write(ws, "b.py")
    extractor = _FakeExtractor(
        {
            "a.py": FileTags("a.py", definitions=(_defn("a", start=1, end=1),)),
            "b.py": FileTags("b.py", definitions=(_defn("b", start=1, end=1),)),
        }
    )
    index = RepoMapIndex(ws, extractor=extractor, cache_path=cache_path)
    out = index.generate(focus=["b.py"], max_tokens=1000)
    assert out.index("b.py") < out.index("a.py")


def test_index_empty_workspace_returns_empty(tmp_path: Path):
    ws, cache_path = _make_workspace(tmp_path)
    extractor = _FakeExtractor({})
    index = RepoMapIndex(ws, extractor=extractor, cache_path=cache_path)
    assert index.generate() == ""


def test_index_extractor_failure_is_fail_open(tmp_path: Path):
    ws, cache_path = _make_workspace(tmp_path)
    _write(ws, "a.py")

    class _Boom:
        identity = "boom:v1"

        def extract(self, relpath, source):
            raise RuntimeError("parser exploded")

    index = RepoMapIndex(ws, extractor=_Boom(), cache_path=cache_path)
    # per-file extraction failure is swallowed -> empty map, no crash
    assert index.generate() == ""
