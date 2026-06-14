"""Tests for skill self-evolution (task 06-01-skill-self-evolution).

Covers the reflection prompt builder (evolve vs create), the canon-name guard
in skill_create, SkillLoader.canon_skill_names, same-name revision via promote,
candidate scoping, and the /skill list revision annotation.
"""

from __future__ import annotations

from pathlib import Path

from bareagent.core.handlers.skill import run_skill_create
from bareagent.main import _print_skill_list
from bareagent.planning.skill_gen import DRAFT_INSTRUCTION, render_reflection_prompt
from bareagent.planning.skill_store import SkillStore
from bareagent.planning.skills import SkillLoader

# --------------------------------------------------------------------------- #
# render_reflection_prompt
# --------------------------------------------------------------------------- #


def test_render_no_candidates_is_bare_instruction():
    # Backward compatible: with nothing to refine, identical to create-only.
    assert render_reflection_prompt([]) == DRAFT_INSTRUCTION


def test_render_with_candidates_lists_and_instructs_refine():
    out = render_reflection_prompt([("deploy-flow", "Use this when deploying."), ("x", "y")])
    assert "REFINE" in out
    assert "deploy-flow: Use this when deploying." in out
    assert "EXACT name" in out
    # Still ends with the base instruction so the decline path survives.
    assert out.endswith(DRAFT_INSTRUCTION)


# --------------------------------------------------------------------------- #
# canon-name guard
# --------------------------------------------------------------------------- #


def test_skill_create_rejects_reserved_canon_name(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    out = run_skill_create(
        store=store,
        name="Git",  # slug -> "git"
        description="Use this when git.",
        body="b",
        reserved_names={"git", "test"},
    )
    assert out.startswith("Error:")
    assert "built-in" in out
    assert store.list_pending() == []  # nothing written


def test_skill_create_allows_non_reserved_name(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    out = run_skill_create(
        store=store,
        name="deploy-flow",
        description="Use this when deploying.",
        body="b",
        reserved_names={"git"},
    )
    assert not out.startswith("Error:")
    assert store.list_pending() == ["deploy-flow"]


def test_skill_create_no_reserved_set_does_not_guard(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    out = run_skill_create(store=store, name="git", description="d", body="b", reserved_names=None)
    assert not out.startswith("Error:")
    assert store.list_pending() == ["git"]


# --------------------------------------------------------------------------- #
# SkillLoader.canon_skill_names
# --------------------------------------------------------------------------- #


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n\n{description}\n", encoding="utf-8")


def test_canon_skill_names_only_repo(tmp_path: Path):
    repo = tmp_path / "repo-skills"
    gen = tmp_path / "gen-skills"
    _write_skill(repo, "alpha", "Repo alpha.")
    _write_skill(gen, "beta", "Generated beta.")
    loader = SkillLoader(repo, generated_root=gen)
    assert loader.canon_skill_names() == {"alpha"}


def test_canon_skill_names_missing_dir_is_empty(tmp_path: Path):
    loader = SkillLoader(tmp_path / "does-not-exist")
    assert loader.canon_skill_names() == set()


# --------------------------------------------------------------------------- #
# same-name revision via promote
# --------------------------------------------------------------------------- #


def test_same_name_revision_replaces_live(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.create_draft("flow", "Use this when flowing.", "VERSION-ONE")
    store.promote("flow")
    loader = SkillLoader(tmp_path / "repo", generated_root=store.root)
    assert "VERSION-ONE" in loader.load("flow")

    # A same-name revision supersedes the live version on promote.
    store.create_draft("flow", "Use this when flowing better.", "VERSION-TWO")
    assert store.list_pending() == ["flow"]
    store.promote("flow")
    loader.scan()
    body = loader.load("flow")
    assert "VERSION-TWO" in body
    assert "VERSION-ONE" not in body


def test_evolution_candidates_exclude_pending_and_canon(tmp_path: Path):
    # The reflection builds candidates from SkillLoader(store.root).scan():
    # generated live only -- pending excluded by the one-level glob, canon
    # excluded by scanning the generated root alone.
    store = SkillStore(tmp_path / "skills")
    store.create_draft("live-one", "Use this when one.", "b")
    store.promote("live-one")
    store.create_draft("pending-two", "Use this when two.", "b")  # stays pending
    candidates = {m.skill_name for m in SkillLoader(store.root).scan()}
    assert candidates == {"live-one"}


# --------------------------------------------------------------------------- #
# /skill list revision annotation
# --------------------------------------------------------------------------- #


class _FakeConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def print_status(self, text: str) -> None:
        self.statuses.append(text)

    def print_error(self, text: str) -> None:  # pragma: no cover - unused here
        self.statuses.append(text)


def test_skill_list_annotates_revisions(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.create_draft("foo", "Use this when foo.", "v1")
    store.promote("foo")  # live foo
    store.create_draft("foo", "Use this when foo better.", "v2")  # pending revision
    store.create_draft("bar", "Use this when bar.", "b")  # pending new
    loader = SkillLoader(store.root)
    console = _FakeConsole()
    _print_skill_list(store, loader, console)
    out = "\n".join(console.statuses)
    assert "(revision of live 'foo')" in out
    assert "- bar" in out
    assert "revision of live 'bar'" not in out
