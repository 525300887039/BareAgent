"""Unit tests for session fork / tree branching (``memory/session_tree.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bareagent.memory.session_tree import (
    ForkRecord,
    enumerate_fork_points,
    load_tree,
    record_fork,
    render_tree,
    slice_for_fork_point,
    tree_path,
)


def _text(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _assistant_tool_use(tool_id: str, name: str = "bash") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
    }


def _user_tool_result(tool_id: str, output: str = "ok") -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}],
    }


# --------------------------------------------------------------------------- #
# enumerate_fork_points
# --------------------------------------------------------------------------- #


def test_enumerate_basic_two_turns() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _text("user", "q1"),
        _text("assistant", "a1"),
        _text("user", "q2"),
        _text("assistant", "a2"),
    ]
    points = enumerate_fork_points(messages)
    assert [p.number for p in points] == [1, 2]
    assert [p.cut for p in points] == [3, 5]
    assert points[0].user_preview == "q1"
    assert points[0].assistant_preview == "a1"
    assert points[1].user_preview == "q2"
    assert points[1].assistant_preview == "a2"


def test_enumerate_skips_mid_tool_cycle_assistant() -> None:
    """An assistant message carrying tool_use is not a boundary; the final
    text assistant after the tool cycle is."""
    messages = [
        {"role": "system", "content": "sys"},
        _text("user", "do it"),
        _assistant_tool_use("t1"),
        _user_tool_result("t1"),
        _text("assistant", "done"),
    ]
    points = enumerate_fork_points(messages)
    assert len(points) == 1
    assert points[0].cut == 5
    assert points[0].user_preview == "do it"
    assert points[0].assistant_preview == "done"


def test_enumerate_compaction_summary_turn_is_a_point() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[Context Compressed]\nold summary"},
        {"role": "assistant", "content": "收到，我已理解之前的上下文，继续工作。"},
        _text("user", "next"),
        _text("assistant", "reply"),
    ]
    points = enumerate_fork_points(messages)
    assert [p.cut for p in points] == [3, 5]
    assert points[0].user_preview.startswith("[Context Compressed]")
    assert points[0].assistant_preview.startswith("收到")


def test_enumerate_only_system_is_empty() -> None:
    assert enumerate_fork_points([{"role": "system", "content": "sys"}]) == []


def test_enumerate_unfinished_first_turn_is_empty() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _text("user", "q"),
        _assistant_tool_use("t1"),
    ]
    assert enumerate_fork_points(messages) == []


def test_enumerate_consecutive_text_assistants() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _text("user", "q"),
        _text("assistant", "a1"),
        _text("assistant", "a2"),
    ]
    points = enumerate_fork_points(messages)
    assert [p.cut for p in points] == [3, 4]
    # both inherit the same preceding real user turn
    assert points[0].user_preview == "q"
    assert points[1].user_preview == "q"


def test_enumerate_string_content_assistant_is_a_point() -> None:
    messages = [_text("user", "q"), {"role": "assistant", "content": "plain"}]
    points = enumerate_fork_points(messages)
    assert len(points) == 1
    assert points[0].assistant_preview == "plain"


def test_enumerate_preview_collapses_and_truncates() -> None:
    long = "word " * 40
    messages = [_text("user", long), _text("assistant", "ok")]
    point = enumerate_fork_points(messages)[0]
    assert "\n" not in point.user_preview
    assert point.user_preview.endswith("…")
    assert len(point.user_preview) <= 61  # limit + ellipsis


# --------------------------------------------------------------------------- #
# slice_for_fork_point
# --------------------------------------------------------------------------- #


def _slice_is_clean(messages: list[dict[str, Any]], sliced: list[dict[str, Any]]) -> bool:
    """The slice ends with a no-tool_use assistant and has no dangling tool_use."""
    if not sliced:
        return False
    last = sliced[-1]
    if last.get("role") != "assistant":
        return False
    content = last.get("content")
    if isinstance(content, list):
        if any(b.get("type") == "tool_use" for b in content if isinstance(b, dict)):
            return False
    return True


def test_slice_returns_clean_prefix_for_every_point() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _text("user", "q1"),
        _assistant_tool_use("t1"),
        _user_tool_result("t1"),
        _text("assistant", "a1"),
        _text("user", "q2"),
        _text("assistant", "a2"),
    ]
    points = enumerate_fork_points(messages)
    assert points  # sanity
    for point in points:
        sliced = slice_for_fork_point(messages, point.number)
        assert len(sliced) == point.cut
        assert _slice_is_clean(messages, sliced)


def test_slice_is_a_deep_copy() -> None:
    messages = [_text("user", "q"), _text("assistant", "a")]
    sliced = slice_for_fork_point(messages, 1)
    sliced[-1]["content"][0]["text"] = "MUTATED"
    # original is untouched
    assert messages[-1]["content"][0]["text"] == "a"


def test_slice_out_of_range_raises() -> None:
    messages = [_text("user", "q"), _text("assistant", "a")]
    with pytest.raises(ValueError, match="out of range"):
        slice_for_fork_point(messages, 5)


def test_slice_no_points_raises() -> None:
    messages = [{"role": "system", "content": "sys"}]
    with pytest.raises(ValueError, match="no fork points"):
        slice_for_fork_point(messages, 1)


# --------------------------------------------------------------------------- #
# lineage sidecar
# --------------------------------------------------------------------------- #


def test_tree_path_is_dot_prefixed(tmp_path: Path) -> None:
    assert tree_path(tmp_path).name == ".tree.json"


def test_record_and_load_round_trip(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    record = ForkRecord(parent="root-1", fork_point=3, parent_len=7, created="2026-06-21T10:00:00Z")
    record_fork(path, "child-1", record)
    loaded = load_tree(path)
    assert loaded == {"child-1": record}


def test_record_fork_overwrites_same_child(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    record_fork(path, "c", ForkRecord("p1", 1, 2, "t1"))
    record_fork(path, "c", ForkRecord("p2", 2, 4, "t2"))
    loaded = load_tree(path)
    assert loaded["c"].parent == "p2"


def test_record_fork_accumulates_multiple_children(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    record_fork(path, "c1", ForkRecord("p", 1, 2, "t"))
    record_fork(path, "c2", ForkRecord("p", 2, 3, "t"))
    assert set(load_tree(path)) == {"c1", "c2"}


def test_load_tree_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_tree(tree_path(tmp_path)) == {}


def test_load_tree_corrupt_json_is_empty(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_tree(path) == {}


def test_load_tree_non_object_is_empty(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_tree(path) == {}


def test_load_tree_skips_bad_entries_keeps_good(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    payload = {
        "good": {"parent": "p", "fork_point": 2, "parent_len": 4, "created": "t"},
        "no-parent": {"fork_point": 1, "parent_len": 2, "created": "t"},
        "bad-types": {"parent": "p2", "fork_point": "x", "parent_len": "y", "created": "t"},
        "": {"parent": "p3", "fork_point": 1, "parent_len": 1, "created": "t"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_tree(path)
    assert set(loaded) == {"good"}
    assert loaded["good"].parent == "p"


def test_record_fork_writes_atomically(tmp_path: Path) -> None:
    path = tree_path(tmp_path)
    record_fork(path, "c", ForkRecord("p", 1, 2, "t"))
    assert path.exists()
    # leaves no temp file behind
    assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# render_tree
# --------------------------------------------------------------------------- #


def test_render_empty_sessions() -> None:
    assert render_tree([], {}, None) == ""


def test_render_flat_when_no_lineage() -> None:
    sessions = ["s2", "s1"]
    out = render_tree(sessions, {}, current="s2")
    lines = out.splitlines()
    assert lines[0] == "s2  ● current"
    assert lines[1] == "s1"


def test_render_multi_level_fork() -> None:
    sessions = ["grandchild", "child1", "root"]
    tree = {
        "child1": ForkRecord("root", 2, 3, "t"),
        "grandchild": ForkRecord("child1", 3, 5, "t"),
    }
    out = render_tree(sessions, tree, current="grandchild")
    lines = out.splitlines()
    assert lines[0] == "root"
    assert lines[1] == "└─ child1  @ turn 2"
    assert lines[2] == "   └─ grandchild  @ turn 3  ● current"


def test_render_orphan_parent_shown_as_root() -> None:
    # child's parent transcript is gone -> child becomes a root, no @ turn marker
    sessions = ["child"]
    tree = {"child": ForkRecord("vanished-parent", 1, 2, "t")}
    out = render_tree(sessions, tree, current=None)
    assert out == "child"


def test_render_sibling_forks() -> None:
    sessions = ["b", "a", "root"]
    tree = {
        "a": ForkRecord("root", 1, 2, "t"),
        "b": ForkRecord("root", 2, 3, "t"),
    }
    out = render_tree(sessions, tree, current=None)
    lines = out.splitlines()
    assert lines[0] == "root"
    # children follow sessions order (b before a)
    assert lines[1] == "├─ b  @ turn 2"
    assert lines[2] == "└─ a  @ turn 1"


def test_render_cycle_does_not_hang() -> None:
    # A corrupt sidecar describing a 2-cycle must not infinite-loop.
    sessions = ["a", "b"]
    tree = {"a": ForkRecord("b", 1, 1, "t"), "b": ForkRecord("a", 1, 1, "t")}
    out = render_tree(sessions, tree, current=None)
    # no root exists in a pure cycle -> empty, but crucially it returns
    assert isinstance(out, str)


def test_render_self_loop_does_not_hang() -> None:
    sessions = ["a"]
    tree = {"a": ForkRecord("a", 1, 1, "t")}
    out = render_tree(sessions, tree, current=None)
    assert isinstance(out, str)
