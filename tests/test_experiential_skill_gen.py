"""Tests for experiential skill generation (task 06-01-experiential-skill-gen).

Covers the pure trigger logic, the draft store (create/promote/discard/list/
prune), SkillLoader multi-root scanning, the skill_create handler, the loop
counting hook, and config parsing.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from src.core.handlers.skill import SKILL_CREATE_TOOL_SCHEMA, run_skill_create
from src.core.loop import agent_loop
from src.permission.guard import PermissionGuard
from src.planning.skill_gen import (
    SkillGenConfig,
    SkillGenerator,
    should_draft_skill,
)
from src.planning.skill_store import (
    SkillStore,
    SkillStoreError,
    derive_skill_slug,
    resolve_generated_skills_root,
)
from src.planning.skills import SkillLoader
from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall

# --------------------------------------------------------------------------- #
# Pure trigger logic
# --------------------------------------------------------------------------- #


def test_should_draft_requires_both_thresholds():
    cfg = SkillGenConfig(enabled=True, min_tool_calls=5, min_user_replies=3)
    assert should_draft_skill(5, 3, cfg) is True
    # Either side short -> no draft.
    assert should_draft_skill(4, 3, cfg) is False
    assert should_draft_skill(5, 2, cfg) is False
    assert should_draft_skill(0, 0, cfg) is False


def test_should_draft_disabled_never_fires():
    cfg = SkillGenConfig(enabled=False, min_tool_calls=1, min_user_replies=1)
    assert should_draft_skill(100, 100, cfg) is False


def test_should_draft_thresholds_configurable():
    cfg = SkillGenConfig(enabled=True, min_tool_calls=2, min_user_replies=1)
    assert should_draft_skill(2, 1, cfg) is True
    assert should_draft_skill(1, 1, cfg) is False


def test_generator_accumulates_and_resets():
    gen = SkillGenerator(SkillGenConfig(min_tool_calls=5, min_user_replies=3))
    gen.note_turn(2)
    gen.note_turn(2)
    assert gen.counters == (4, 2)
    assert gen.should_draft() is False
    gen.note_turn(1)  # tool_calls=5, user_replies=3 -> crosses
    assert gen.counters == (5, 3)
    assert gen.should_draft() is True
    gen.reset()
    assert gen.counters == (0, 0)
    assert gen.should_draft() is False


def test_generator_disabled_does_not_count():
    gen = SkillGenerator(SkillGenConfig(enabled=False))
    gen.note_turn(10)
    assert gen.counters == (0, 0)
    assert gen.enabled is False


def test_generator_negative_tool_count_clamped():
    gen = SkillGenerator(SkillGenConfig(min_tool_calls=1, min_user_replies=1))
    gen.note_turn(-5)
    assert gen.counters == (0, 1)


# --------------------------------------------------------------------------- #
# derive_skill_slug
# --------------------------------------------------------------------------- #


def test_slug_neutralizes_traversal_and_spaces():
    assert derive_skill_slug("My Cool Skill") == "my-cool-skill"
    assert derive_skill_slug("../../etc/passwd") == "etc-passwd"
    assert derive_skill_slug("a/b\\c") == "a-b-c"


def test_slug_empty_for_punctuation_only():
    assert derive_skill_slug("///") == ""
    assert derive_skill_slug("   ") == ""


# --------------------------------------------------------------------------- #
# SkillStore
# --------------------------------------------------------------------------- #


def test_create_draft_writes_loader_parsable_skill(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    msg = store.create_draft("My Skill", "Use this when testing.", "## Steps\n1. do it")
    assert "my-skill" in msg
    draft = tmp_path / "skills" / ".pending" / "my-skill" / "SKILL.md"
    assert draft.exists()
    # Description must be the first non-#, non-empty line for SkillLoader.
    loader = SkillLoader(tmp_path / "nonexistent-repo", generated_root=store.pending_root)
    metas = {m.skill_name: m for m in loader.scan()}
    assert metas["my-skill"].description == "Use this when testing."


def test_create_draft_rejects_empty_name(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    try:
        store.create_draft("///", "d", "b")
    except SkillStoreError:
        pass
    else:  # pragma: no cover - explicit failure
        raise AssertionError("expected SkillStoreError for empty slug")


def test_promote_moves_draft_to_live(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.create_draft("alpha", "Use this when alpha.", "body")
    assert store.list_pending() == ["alpha"]
    assert store.list_live() == []
    store.promote("alpha")
    assert store.list_pending() == []
    assert store.list_live() == ["alpha"]
    assert (tmp_path / "skills" / "alpha" / "SKILL.md").exists()


def test_promote_missing_draft_raises(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    try:
        store.promote("ghost")
    except SkillStoreError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected SkillStoreError for missing draft")


def test_discard_removes_draft(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.create_draft("beta", "Use this when beta.", "body")
    store.discard("beta")
    assert store.list_pending() == []


def test_prune_pending_keeps_newest(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    # Create 4 drafts with strictly increasing mtimes so ordering is deterministic.
    import os
    import time

    for i in range(4):
        store.create_draft(f"skill-{i}", f"Use this when {i}.", "body")
        path = store.pending_root / f"skill-{i}" / "SKILL.md"
        stamp = time.time() + i  # strictly increasing
        os.utime(path, (stamp, stamp))
    removed = store.prune_pending(max_pending=2)
    assert set(removed) == {"skill-0", "skill-1"}
    assert store.list_pending() == ["skill-2", "skill-3"]


def test_prune_pending_disabled_when_nonpositive(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.create_draft("only", "Use this when only.", "body")
    assert store.prune_pending(0) == []
    assert store.list_pending() == ["only"]


def test_resolve_generated_root_default_and_override(tmp_path: Path):
    default = resolve_generated_skills_root(tmp_path, "")
    assert default.parts[-1] == "skills"
    assert ".bareagent" in default.parts
    # Relative override is workspace-relative.
    rel = resolve_generated_skills_root(tmp_path, "gen-skills")
    assert rel == tmp_path / "gen-skills"
    # Absolute override is used as-is.
    abs_dir = tmp_path / "abs"
    assert resolve_generated_skills_root(tmp_path, str(abs_dir)) == abs_dir


# --------------------------------------------------------------------------- #
# SkillLoader multi-root
# --------------------------------------------------------------------------- #


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n\n{description}\n", encoding="utf-8")


def test_loader_lists_both_roots(tmp_path: Path):
    repo = tmp_path / "repo-skills"
    gen = tmp_path / "gen-skills"
    _write_skill(repo, "alpha", "Repo alpha.")
    _write_skill(gen, "beta", "Generated beta.")
    loader = SkillLoader(repo, generated_root=gen)
    names = {m.skill_name for m in loader.scan()}
    assert names == {"alpha", "beta"}
    # Both are loadable.
    assert "Repo alpha." in loader.load("alpha")
    assert "Generated beta." in loader.load("beta")


def test_loader_repo_wins_on_name_conflict(tmp_path: Path):
    repo = tmp_path / "repo-skills"
    gen = tmp_path / "gen-skills"
    _write_skill(repo, "dup", "Canonical repo version.")
    _write_skill(gen, "dup", "Generated shadow version.")
    loader = SkillLoader(repo, generated_root=gen)
    metas = {m.skill_name: m for m in loader.scan()}
    assert metas["dup"].description == "Canonical repo version."


def test_loader_ignores_pending_drafts(tmp_path: Path):
    gen = tmp_path / "gen-skills"
    store = SkillStore(gen)
    store.create_draft("draft-only", "Use this when drafting.", "body")
    loader = SkillLoader(tmp_path / "repo-skills", generated_root=gen)
    names = {m.skill_name for m in loader.scan()}
    assert "draft-only" not in names
    # After promotion it becomes loadable.
    store.promote("draft-only")
    loader.scan()
    assert "draft-only" in {m.skill_name for m in loader.scan()}


# --------------------------------------------------------------------------- #
# skill_create handler
# --------------------------------------------------------------------------- #


def test_run_skill_create_success(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    out = run_skill_create(
        store=store, name="demo", description="Use this when demo.", body="steps"
    )
    assert "demo" in out
    assert store.list_pending() == ["demo"]


def test_run_skill_create_empty_name_returns_error(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    out = run_skill_create(store=store, name="  ", description="d", body="b")
    assert out.startswith("Error:")
    assert store.list_pending() == []


def test_skill_create_schema_shape():
    assert SKILL_CREATE_TOOL_SCHEMA["name"] == "skill_create"
    props = SKILL_CREATE_TOOL_SCHEMA["parameters"]["properties"]
    assert set(props) == {"name", "description", "body"}


# --------------------------------------------------------------------------- #
# Permission
# --------------------------------------------------------------------------- #


def test_skill_create_is_safe_tool():
    assert "skill_create" in PermissionGuard.SAFE_TOOLS


# --------------------------------------------------------------------------- #
# Loop integration
# --------------------------------------------------------------------------- #


class _ScriptedProvider(BaseLLMProvider):
    model = "test-model"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        return self._responses.pop(0)

    def create_stream(
        self, messages, tools, **kwargs
    ) -> Generator[Any, None, LLMResponse]:  # pragma: no cover - non-streaming test
        raise NotImplementedError


def test_agent_loop_notes_turn_tool_count():
    provider = _ScriptedProvider(
        [
            LLMResponse(
                text="",
                stop_reason="tool_use",
                input_tokens=1,
                output_tokens=1,
                tool_calls=[ToolCall(id="t1", name="echo", input={"x": "hi"})],
            ),
            LLMResponse(text="done", stop_reason="end_turn", input_tokens=1, output_tokens=1),
        ]
    )
    gen = SkillGenerator(SkillGenConfig(min_tool_calls=1, min_user_replies=1))
    handlers: dict[str, Any] = {"echo": lambda x: x}
    result = agent_loop(
        provider=provider,
        messages=[{"role": "user", "content": "go"}],
        tools=[],
        handlers=handlers,
        stream=False,
        skill_gen=gen,
    )
    assert result == "done"
    assert gen.counters == (1, 1)
    assert gen.should_draft() is True


def test_agent_loop_without_skill_gen_is_noop():
    provider = _ScriptedProvider(
        [LLMResponse(text="hi", stop_reason="end_turn", input_tokens=1, output_tokens=1)]
    )
    # No skill_gen passed -> no counting, no error.
    result = agent_loop(
        provider=provider,
        messages=[{"role": "user", "content": "go"}],
        tools=[],
        handlers={},
        stream=False,
    )
    assert result == "hi"


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #


def test_parse_skills_config_defaults():
    from src.main import SkillsConfig, _parse_skills_config

    cfg = _parse_skills_config({})
    assert cfg == SkillsConfig()
    assert cfg.auto_generate is True
    assert cfg.min_tool_calls == 5
    assert cfg.min_user_replies == 3
    assert cfg.max_pending == 10


def test_parse_skills_config_env_override(monkeypatch):
    from src.main import _parse_skills_config

    monkeypatch.setenv("BAREAGENT_SKILLS_AUTO_GENERATE", "false")
    cfg = _parse_skills_config({"auto_generate": True})
    assert cfg.auto_generate is False


def test_parse_skills_config_bad_field_falls_back():
    from src.main import _parse_skills_config

    cfg = _parse_skills_config({"min_tool_calls": "not-an-int", "max_pending": None})
    assert cfg.min_tool_calls == 5
    assert cfg.max_pending == 10


def test_build_skillgen_config_maps_fields():
    from src.main import SkillsConfig, _build_skillgen_config

    skills = SkillsConfig(auto_generate=False, min_tool_calls=7, min_user_replies=2, max_pending=3)
    gen_cfg = _build_skillgen_config(skills)
    assert gen_cfg.enabled is False
    assert gen_cfg.min_tool_calls == 7
    assert gen_cfg.min_user_replies == 2
