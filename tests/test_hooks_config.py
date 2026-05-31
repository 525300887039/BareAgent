"""Tests for src.hooks.config — TOML-derived hook configuration parsing."""

from __future__ import annotations

import pytest

from src.hooks.config import HookEntry, HooksConfig, parse_hooks_config
from src.hooks.errors import HookConfigError


def _wrap(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"hooks": entries}


def test_parses_pre_and_post_entries() -> None:
    cfg = parse_hooks_config(
        _wrap(
            [
                {
                    "event": "PreToolUse",
                    "tool": "bash",
                    "command": "block.sh",
                    "timeout": 5,
                },
                {
                    "event": "PostToolUse",
                    "command": "format.sh",
                },
            ]
        )
    )
    assert isinstance(cfg, HooksConfig)
    assert cfg.skipped == []
    assert len(cfg.entries) == 2

    pre = cfg.entries[0]
    assert pre.event == "PreToolUse"
    assert pre.tool == "bash"
    assert pre.command == "block.sh"
    assert pre.timeout == 5

    post = cfg.entries[1]
    assert post.event == "PostToolUse"
    assert post.tool is None  # omitted => match all tools
    assert post.timeout == 30  # default


def test_accepts_full_document_with_hooks_key() -> None:
    cfg = parse_hooks_config(
        {"hooks": [{"event": "PreToolUse", "command": "x.sh"}], "other": {"k": "v"}}
    )
    assert len(cfg.entries) == 1


def test_empty_config_yields_no_entries() -> None:
    assert parse_hooks_config({}).entries == []
    assert parse_hooks_config({"hooks": []}).entries == []


def test_unknown_event_is_skipped_not_fatal() -> None:
    cfg = parse_hooks_config(
        _wrap(
            [
                {"event": "Stop", "command": "x.sh"},
                {"event": "PreToolUse", "command": "ok.sh"},
            ]
        )
    )
    # Only the valid entry survives; the bad one is recorded.
    assert [e.command for e in cfg.entries] == ["ok.sh"]
    assert len(cfg.skipped) == 1
    assert "event" in cfg.skipped[0]


def test_missing_or_blank_command_is_skipped() -> None:
    cfg = parse_hooks_config(
        _wrap(
            [
                {"event": "PreToolUse"},
                {"event": "PreToolUse", "command": "   "},
                {"event": "PreToolUse", "command": 123},
                {"event": "PreToolUse", "command": "good.sh"},
            ]
        )
    )
    assert [e.command for e in cfg.entries] == ["good.sh"]
    assert len(cfg.skipped) == 3


def test_invalid_tool_or_timeout_is_skipped() -> None:
    cfg = parse_hooks_config(
        _wrap(
            [
                {"event": "PreToolUse", "command": "x.sh", "tool": ""},
                {"event": "PreToolUse", "command": "x.sh", "tool": 5},
                {"event": "PreToolUse", "command": "x.sh", "timeout": 0},
                {"event": "PreToolUse", "command": "x.sh", "timeout": True},
                {"event": "PreToolUse", "command": "x.sh", "timeout": "10"},
            ]
        )
    )
    assert cfg.entries == []
    assert len(cfg.skipped) == 5


def test_non_table_entry_is_skipped() -> None:
    cfg = parse_hooks_config(_wrap(["not-a-table", {"event": "PreToolUse", "command": "ok.sh"}]))  # type: ignore[list-item]
    assert [e.command for e in cfg.entries] == ["ok.sh"]
    assert len(cfg.skipped) == 1


def test_structural_error_raises() -> None:
    with pytest.raises(HookConfigError):
        parse_hooks_config({"hooks": "not-a-list"})
    with pytest.raises(HookConfigError):
        parse_hooks_config("not-a-dict")  # type: ignore[arg-type]


def test_matching_filters_by_event_and_tool_preserving_order() -> None:
    cfg = HooksConfig(
        entries=[
            HookEntry(event="PreToolUse", command="a", tool="bash"),
            HookEntry(event="PreToolUse", command="b", tool=None),
            HookEntry(event="PreToolUse", command="c", tool="write_file"),
            HookEntry(event="PostToolUse", command="d", tool="bash"),
        ]
    )

    bash_pre = cfg.matching("PreToolUse", "bash")
    # entry a (tool=bash) and entry b (tool=None) match, in declaration order.
    assert [e.command for e in bash_pre] == ["a", "b"]

    write_pre = cfg.matching("PreToolUse", "write_file")
    assert [e.command for e in write_pre] == ["b", "c"]

    bash_post = cfg.matching("PostToolUse", "bash")
    assert [e.command for e in bash_post] == ["d"]

    assert cfg.matching("PreToolUse", "glob") == [cfg.entries[1]]  # only tool=None
