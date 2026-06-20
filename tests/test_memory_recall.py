"""Unit tests for the memory recall layer (src/memory/persistent.py recall +
src/main.py:_refresh_memory_recall)."""

from __future__ import annotations

from pathlib import Path

from bareagent.main import (
    MemoryConfig,
    _refresh_memory_recall,
    load_config,
)
from bareagent.memory.persistent import (
    MemoryManager,
    parse_frontmatter,
)


def _manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(tmp_path / "memory")


def _write(mm: MemoryManager, rel: str, name: str, description: str, body: str = "body") -> None:
    text = f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: user\n---\n{body}\n"
    mm.create(rel, text)


# -- parse_frontmatter ----------------------------------------------------


def test_parse_frontmatter_extracts_top_level_keys():
    text = (
        "---\n"
        "name: my-slug\n"
        "description: a one line summary\n"
        "metadata:\n"
        "  type: user\n"
        "---\n"
        "the body\n"
    )
    meta = parse_frontmatter(text)
    assert meta["name"] == "my-slug"
    assert meta["description"] == "a one line summary"
    # Nested ``metadata:`` block (indented) is ignored.
    assert "type" not in meta


def test_parse_frontmatter_no_frontmatter_returns_empty():
    assert parse_frontmatter("just a plain body\nno fence") == {}


def test_parse_frontmatter_unclosed_fence_returns_empty():
    assert parse_frontmatter("---\nname: x\nstill open, no closing fence") == {}


def test_parse_frontmatter_does_not_raise_on_malformed():
    # A bare ``key:`` line with no value is skipped, not an error.
    meta = parse_frontmatter("---\nname: ok\nbroken\n---\nbody")
    assert meta == {"name": "ok"}


# -- recall ----------------------------------------------------------------


def test_recall_orders_by_lexical_overlap_and_takes_top_k(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "deploy pipeline and docker build")
    _write(mm, "b.md", "beta", "deploy docker container settings")
    _write(mm, "c.md", "gamma", "favorite editor and theme")
    hits = mm.recall("docker deploy", k=2)
    assert len(hits) == 2
    # Both deploy/docker entries outrank the unrelated editor entry.
    paths = [h.path for h in hits]
    assert "c.md" not in paths
    # Highest score first.
    assert hits[0].score >= hits[1].score


def test_recall_excludes_memory_index(tmp_path):
    mm = _manager(tmp_path)
    mm.create("MEMORY.md", "- [docker](a.md) — docker deploy notes")
    _write(mm, "a.md", "alpha", "docker deploy notes")
    hits = mm.recall("docker", k=5)
    assert all(h.path != "MEMORY.md" for h in hits)
    assert [h.path for h in hits] == ["a.md"]


def test_recall_empty_query_returns_empty(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    assert mm.recall("", k=5) == []
    assert mm.recall("   ", k=5) == []


def test_recall_no_match_returns_empty(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    assert mm.recall("completely unrelated zzz", k=5) == []


def test_recall_falls_back_to_body_when_frontmatter_missing(tmp_path):
    mm = _manager(tmp_path)
    mm.create("plain.md", "kubernetes orchestration cluster notes")
    hits = mm.recall("kubernetes cluster", k=5)
    assert [h.path for h in hits] == ["plain.md"]


def test_recall_matches_chinese_query(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "zh.md", "用户偏好", "用户喜欢深色主题和中文回复")
    _write(mm, "en.md", "editor", "favorite code editor settings")
    hits = mm.recall("深色主题", k=5)
    assert [h.path for h in hits] == ["zh.md"]


def test_recall_matches_english_query(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "zh.md", "用户偏好", "用户喜欢深色主题和中文回复")
    _write(mm, "en.md", "editor", "favorite code editor settings")
    hits = mm.recall("editor settings", k=5)
    assert [h.path for h in hits] == ["en.md"]


# -- recall_section --------------------------------------------------------


def test_recall_section_includes_tag_and_paths(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    section = mm.recall_section("docker deploy", k=5)
    assert section.startswith("<memory-recall>")
    assert section.endswith("</memory-recall>")
    assert "a.md" in section
    assert "docker deploy notes" in section


def test_recall_section_empty_when_no_match(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    assert mm.recall_section("nothing relevant zzz", k=5) == ""


# -- _refresh_memory_recall ------------------------------------------------


def _recall_messages(messages: list[dict]) -> list[dict]:
    return [
        m
        for m in messages
        if m.get("role") == "system"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("<memory-recall>")
    ]


def test_refresh_memory_recall_injects_after_user(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "how do I docker deploy?"},
    ]
    _refresh_memory_recall(messages, mm, recall_k=5)
    recalls = _recall_messages(messages)
    assert len(recalls) == 1
    # Inserted right after the user message.
    user_index = messages.index({"role": "user", "content": "how do I docker deploy?"})
    assert messages[user_index + 1] is recalls[0]


def test_refresh_memory_recall_replaces_stale_block(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    _write(mm, "b.md", "beta", "kubernetes cluster setup")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "docker deploy"},
    ]
    _refresh_memory_recall(messages, mm, recall_k=5)
    # New user turn with a different topic.
    messages.append({"role": "user", "content": "kubernetes cluster"})
    _refresh_memory_recall(messages, mm, recall_k=5)
    recalls = _recall_messages(messages)
    assert len(recalls) == 1
    assert "b.md" in recalls[0]["content"]
    assert "a.md" not in recalls[0]["content"]


def test_refresh_memory_recall_removes_compaction_relocated_block(tmp_path):
    # Full compaction (src/memory/compact.py) preserves every system message and
    # re-emits them at the front, so a previously-injected <memory-recall> block
    # survives detached from "after the last user message". The next refresh must
    # still strip it by prefix before injecting a fresh one — otherwise recall
    # blocks accumulate across rounds.
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    messages = [
        {"role": "system", "content": "sys"},
        # Stale recall block sitting at the front, as compaction would leave it.
        {"role": "system", "content": "<memory-recall>\nstale\n</memory-recall>"},
        {"role": "user", "content": "[Context Compressed]\nsummary"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "docker deploy"},
    ]
    _refresh_memory_recall(messages, mm, recall_k=5)
    recalls = _recall_messages(messages)
    # Exactly one block, freshly placed after the latest user message — no carry-over.
    assert len(recalls) == 1
    assert "stale" not in recalls[0]["content"]
    assert "a.md" in recalls[0]["content"]
    user_index = messages.index({"role": "user", "content": "docker deploy"})
    assert messages[user_index + 1] is recalls[0]


def test_refresh_memory_recall_disabled_when_manager_none(tmp_path):
    messages = [
        {"role": "system", "content": "<memory-recall>\nold\n</memory-recall>"},
        {"role": "user", "content": "docker deploy"},
    ]
    _refresh_memory_recall(messages, None, recall_k=5)
    assert _recall_messages(messages) == []


def test_refresh_memory_recall_disabled_when_recall_k_zero(tmp_path):
    mm = _manager(tmp_path)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    messages = [
        {"role": "system", "content": "<memory-recall>\nold\n</memory-recall>"},
        {"role": "user", "content": "docker deploy"},
    ]
    _refresh_memory_recall(messages, mm, recall_k=0)
    assert _recall_messages(messages) == []


# -- config ----------------------------------------------------------------


def test_memory_config_recall_k_default():
    assert MemoryConfig().recall_k == 5


def test_load_config_parses_recall_k(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        "[memory]\nrecall_k = 3\n",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.memory.recall_k == 3


def test_load_config_parses_semantic_recall_fields(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        '[memory]\nsemantic_recall = true\nembedding_backend = "local"\n'
        'embedding_model = "BAAI/bge-small-en-v1.5"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.memory.semantic_recall is True
    assert cfg.memory.embedding_backend == "local"
    assert cfg.memory.embedding_model == "BAAI/bge-small-en-v1.5"


def test_memory_config_semantic_recall_defaults_off():
    cfg = MemoryConfig()
    assert cfg.semantic_recall is False
    assert cfg.embedding_backend == "openai"


def test_build_memory_embedder_fails_open_when_provider_has_no_api_key(tmp_path):
    # Regression: _resolve_api_key raises ValueError when the provider config
    # has neither api_key nor api_key_env. Since it is evaluated as a call
    # argument before build_embedder's own try/except runs, that ValueError
    # must not escape and crash boot — semantic recall is fail-open.
    import dataclasses

    from bareagent.main import _build_memory_embedder
    from bareagent.ui.console import AgentConsole
    from tests.conftest import make_test_config

    base = make_test_config(tmp_path)
    config = dataclasses.replace(
        base,
        provider=dataclasses.replace(base.provider, api_key_env="", api_key=""),
        memory=MemoryConfig(semantic_recall=True, embedding_backend="openai"),
    )
    # No raise; the openai client with an empty key cannot embed, so it degrades.
    embedder = _build_memory_embedder(config, AgentConsole())
    # Either the build itself returned None, or it produced a client that will
    # fail at embed-time (recall catches that and falls back). The contract
    # tested here is only that boot did not crash.
    assert embedder is None or hasattr(embedder, "embed")


# -- semantic recall (src/memory/embedding.py injected backend) ------------


class _FakeEmbedder:
    """Deterministic topic-vector embedder: deploy / editor axes + bias.

    Lets a query share *zero* lexical terms with a memory yet still rank it
    first by topic — exactly the case lexical recall misses.
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
        deploy_terms = ("deploy", "docker", "container", "ship", "release", "部署", "容器")
        editor_terms = ("editor", "theme", "font", "主题", "字体")
        deploy = 1.0 if any(term in text or term in lowered for term in deploy_terms) else 0.0
        editor = 1.0 if any(term in lowered for term in editor_terms) else 0.0
        return [deploy, editor, 0.1]


def test_semantic_recall_matches_paraphrase_lexical_would_miss(tmp_path):
    mm_lexical = _manager(tmp_path / "lex")
    _write(mm_lexical, "a.md", "alpha", "部署 docker 容器")
    _write(mm_lexical, "b.md", "beta", "favorite editor 主题")
    # A query that shares no lexical terms with the Chinese deploy memory.
    assert mm_lexical.recall("ship a release to production", k=5) == []

    embedder = _FakeEmbedder()
    mm = MemoryManager(tmp_path / "sem", embedder=embedder)
    _write(mm, "a.md", "alpha", "部署 docker 容器")
    _write(mm, "b.md", "beta", "favorite editor 主题")
    hits = mm.recall("ship a release to production", k=5)
    # Pure top-K cosine ranking: the deploy memory the query paraphrases ranks
    # first (lexical recall returned nothing at all above).
    assert hits[0].path == "a.md"
    assert hits[0].score > hits[1].score


def test_semantic_recall_falls_back_to_lexical_on_embed_error(tmp_path):
    embedder = _FakeEmbedder()
    embedder.raise_on_embed = True
    mm = MemoryManager(tmp_path / "sem", embedder=embedder)
    _write(mm, "a.md", "alpha", "docker deploy notes")
    # Embedder raises -> recall must fall back to lexical, not crash.
    hits = mm.recall("docker deploy", k=5)
    assert [h.path for h in hits] == ["a.md"]


def test_semantic_recall_caches_doc_embeddings(tmp_path):
    embedder = _FakeEmbedder()
    mm = MemoryManager(tmp_path / "sem", embedder=embedder)
    _write(mm, "a.md", "alpha", "部署 docker 容器")
    _write(mm, "b.md", "beta", "favorite editor 主题")

    mm.recall("ship release", k=5)
    after_first = len(embedder.calls)  # one doc batch + one query batch == 2
    mm.recall("ship release", k=5)
    # Second call re-embeds only the query (docs served from the on-disk cache).
    assert after_first == 2
    assert len(embedder.calls) == 3
    assert embedder.calls[-1] == ["ship release"]


def test_semantic_recall_reembeds_on_content_change(tmp_path):
    embedder = _FakeEmbedder()
    mm = MemoryManager(tmp_path / "sem", embedder=embedder)
    _write(mm, "a.md", "alpha", "部署 docker 容器")

    mm.recall("ship release", k=5)
    # Change the memory's content -> its hash changes -> it must be re-embedded.
    _write(mm, "a.md", "alpha", "editor 主题 settings now")
    embedder.calls.clear()
    mm.recall("ship release", k=5)
    embedded = [t for call in embedder.calls for t in call]
    assert any("editor 主题 settings now" in t for t in embedded)
