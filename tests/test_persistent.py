"""Unit tests for the persistent memory store (src/memory/persistent.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bareagent.memory.persistent import (
    MEMORY_PROTOCOL,
    MemoryManager,
    MemoryType,
    build_forget_instruction,
    build_remember_instruction,
    default_memory_root,
    derive_memory_slug,
    resolve_memory_root,
)


def _manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(tmp_path / "memory")


# -- create / view --------------------------------------------------------


def test_create_then_view_returns_numbered_lines(tmp_path):
    mm = _manager(tmp_path)
    mm.create("user/role.md", "line one\nline two\nline three")
    out = mm.view("user/role.md")
    assert out == "1\tline one\n2\tline two\n3\tline three"


def test_create_writes_file_under_root(tmp_path):
    mm = _manager(tmp_path)
    result = mm.create("note.md", "hello")
    assert (mm.root / "note.md").read_text(encoding="utf-8") == "hello"
    assert "note.md" in result


def test_view_directory_lists_entries(tmp_path):
    mm = _manager(tmp_path)
    mm.create("MEMORY.md", "index")
    mm.create("user/role.md", "x")
    listing = mm.view(".")
    assert "Memory root:" in listing
    assert "- MEMORY.md" in listing
    assert "- user/" in listing


def test_view_empty_root(tmp_path):
    mm = _manager(tmp_path)
    assert "(empty)" in mm.view(".")


def test_view_missing_file_raises(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mm.view("nope.md")


def test_view_range_slices_lines(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb\nc\nd")
    assert mm.view("f.md", view_range=[2, 3]) == "2\tb\n3\tc"


def test_view_range_to_end_with_minus_one(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb\nc")
    assert mm.view("f.md", view_range=[2, -1]) == "2\tb\n3\tc"


def test_view_range_out_of_range_raises(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb")
    with pytest.raises(ValueError):
        mm.view("f.md", view_range=[5, 6])


# -- str_replace ----------------------------------------------------------


def test_str_replace_unique(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "alpha beta gamma")
    mm.str_replace("f.md", "beta", "BETA")
    assert (mm.root / "f.md").read_text(encoding="utf-8") == "alpha BETA gamma"


def test_str_replace_not_found_raises(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "alpha")
    with pytest.raises(ValueError, match="not found"):
        mm.str_replace("f.md", "zeta", "Z")


def test_str_replace_non_unique_raises(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "x x x")
    with pytest.raises(ValueError, match="not unique"):
        mm.str_replace("f.md", "x", "y")


def test_str_replace_missing_file_raises(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mm.str_replace("ghost.md", "a", "b")


# -- insert ---------------------------------------------------------------


def test_insert_after_line(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb\nc")
    mm.insert("f.md", 1, "X")
    assert (mm.root / "f.md").read_text(encoding="utf-8") == "a\nX\nb\nc"


def test_insert_at_start(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb")
    mm.insert("f.md", 0, "head")
    assert (mm.root / "f.md").read_text(encoding="utf-8") == "head\na\nb"


def test_insert_out_of_range_raises(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "a\nb")
    with pytest.raises(ValueError):
        mm.insert("f.md", 99, "x")


# -- delete / rename ------------------------------------------------------


def test_delete_file(tmp_path):
    mm = _manager(tmp_path)
    mm.create("f.md", "x")
    mm.delete("f.md")
    assert not (mm.root / "f.md").exists()


def test_delete_directory_recursive(tmp_path):
    mm = _manager(tmp_path)
    mm.create("sub/a.md", "x")
    mm.delete("sub")
    assert not (mm.root / "sub").exists()


def test_delete_root_refused(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(ValueError, match="root"):
        mm.delete(".")


def test_delete_missing_raises(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mm.delete("ghost.md")


def test_rename_moves_file(tmp_path):
    mm = _manager(tmp_path)
    mm.create("old.md", "x")
    mm.rename("old.md", "user/new.md")
    assert not (mm.root / "old.md").exists()
    assert (mm.root / "user" / "new.md").read_text(encoding="utf-8") == "x"


def test_rename_to_existing_raises(tmp_path):
    mm = _manager(tmp_path)
    mm.create("a.md", "1")
    mm.create("b.md", "2")
    with pytest.raises(ValueError, match="exists"):
        mm.rename("a.md", "b.md")


def test_rename_missing_source_raises(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mm.rename("ghost.md", "x.md")


# -- path safety ----------------------------------------------------------


def test_parent_traversal_rejected(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(PermissionError):
        mm.view("../../etc/passwd")


def test_home_path_rejected(tmp_path):
    mm = _manager(tmp_path)
    with pytest.raises(PermissionError):
        mm.create("~/secret.md", "x")


def test_memories_prefix_is_stripped(tmp_path):
    mm = _manager(tmp_path)
    mm.create("/memories/user/role.md", "x")
    # Resolves under the root, with the native /memories/ prefix removed.
    assert (mm.root / "user" / "role.md").exists()


def test_absolute_path_is_confined_not_escaped(tmp_path):
    mm = _manager(tmp_path)
    # A leading slash is stripped to a relative path; it can never escape root.
    mm.create("/abs.md", "x")
    assert (mm.root / "abs.md").exists()


# -- system prompt section ------------------------------------------------


def test_system_prompt_section_empty(tmp_path):
    mm = _manager(tmp_path)
    section = mm.system_prompt_section()
    assert "<memory>" in section
    assert MEMORY_PROTOCOL in section
    assert "(no memories saved yet)" in section


def test_system_prompt_section_includes_index(tmp_path):
    mm = _manager(tmp_path)
    mm.create("MEMORY.md", "- [Role](user/role.md) — senior dev")
    section = mm.system_prompt_section()
    assert "user/role.md" in section
    assert '<memory-index file="MEMORY.md">' in section


def test_system_prompt_section_truncates_index(tmp_path):
    mm = MemoryManager(tmp_path / "memory", max_index_lines=2)
    mm.create("MEMORY.md", "l1\nl2\nl3\nl4")
    section = mm.system_prompt_section()
    assert "l1" in section and "l2" in section
    assert "l3" not in section


# -- slug / root resolution ----------------------------------------------


def test_derive_memory_slug_is_filesystem_safe():
    slug = derive_memory_slug(Path("/tmp/some/proj"))
    assert "/" not in slug and "\\" not in slug and ":" not in slug
    assert slug


def test_default_memory_root_is_per_project(tmp_path):
    root_a = default_memory_root(tmp_path / "a")
    root_b = default_memory_root(tmp_path / "b")
    assert root_a != root_b
    assert root_a.name == "memory"


def test_resolve_memory_root_empty_uses_default(tmp_path):
    assert resolve_memory_root(tmp_path, "") == default_memory_root(tmp_path)


def test_resolve_memory_root_relative_under_workspace(tmp_path):
    assert resolve_memory_root(tmp_path, ".mem") == tmp_path / ".mem"


def test_resolve_memory_root_absolute_kept(tmp_path):
    target = tmp_path / "abs_mem"
    assert resolve_memory_root(tmp_path, str(target)) == target


# -- instruction builders & enum -----------------------------------------


def test_memory_type_values():
    assert {t.value for t in MemoryType} == {
        "user",
        "feedback",
        "project",
        "reference",
    }


def test_remember_instruction_with_text_embeds_payload():
    instr = build_remember_instruction("user prefers tabs")
    assert "user prefers tabs" in instr
    assert "memory" in instr.lower()


def test_remember_instruction_without_text_reviews_conversation():
    instr = build_remember_instruction("")
    assert "memory" in instr.lower()
    assert "user prefers tabs" not in instr


def test_forget_instruction_with_text_embeds_payload():
    instr = build_forget_instruction("the old API key")
    assert "the old API key" in instr
    assert "delete" in instr.lower()
