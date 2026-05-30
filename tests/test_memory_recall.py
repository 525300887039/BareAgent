"""Unit tests for the memory recall layer (src/memory/persistent.py recall +
src/main.py:_refresh_memory_recall)."""

from __future__ import annotations

from pathlib import Path

from src.main import (
    MemoryConfig,
    _refresh_memory_recall,
    load_config,
)
from src.memory.persistent import (
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
