from __future__ import annotations

import re

from bareagent.core.handlers.subagent_send import (
    SUBAGENT_SEND_TOOL_SCHEMA,
    run_subagent_send,
)
from bareagent.permission.guard import PermissionGuard, PermissionMode
from bareagent.planning.agent_types import MAIN_LOOP_ONLY_TOOLS, filter_tools, resolve_agent_type
from bareagent.planning.subagent import run_subagent
from bareagent.planning.subagent_registry import (
    DEFAULT_MAX_RESUMABLE,
    ResumableContext,
    SubagentRegistry,
)

_FOOTNOTE_ID = re.compile(r"subagent id (sa-\w+):")


def _ctx(agent_id: str, messages: list | None = None) -> ResumableContext:
    return ResumableContext(
        agent_id=agent_id,
        messages=messages if messages is not None else [],
        provider=object(),
        tools=[],
        handlers={},
        permission=None,
        compact_fn=lambda _messages: None,
        max_turns=50,
        retry_policy=None,
    )


# --------------------------------------------------------------------------- #
# SubagentRegistry                                                            #
# --------------------------------------------------------------------------- #
def test_registry_register_get_has_clear() -> None:
    reg = SubagentRegistry()
    ctx = _ctx("sa-1")
    reg.register(ctx)
    assert reg.has("sa-1")
    assert reg.get("sa-1") is ctx
    assert reg.get("sa-missing") is None
    assert len(reg) == 1
    reg.clear()
    assert len(reg) == 0
    assert not reg.has("sa-1")


def test_registry_fifo_evicts_oldest() -> None:
    reg = SubagentRegistry(max_resumable=2)
    reg.register(_ctx("sa-1"))
    reg.register(_ctx("sa-2"))
    reg.register(_ctx("sa-3"))
    # Oldest (sa-1) is evicted once we exceed the cap of 2.
    assert not reg.has("sa-1")
    assert reg.has("sa-2")
    assert reg.has("sa-3")
    assert len(reg) == 2


def test_registry_touch_moves_to_end_and_protects_active() -> None:
    reg = SubagentRegistry(max_resumable=2)
    ctx1 = _ctx("sa-1")
    reg.register(ctx1)
    reg.register(_ctx("sa-2"))
    # Re-registering sa-1 refreshes its position; sa-2 is now the oldest.
    reg.register(ctx1)
    reg.register(_ctx("sa-3"))
    assert reg.has("sa-1")
    assert not reg.has("sa-2")
    assert reg.has("sa-3")


def test_registry_generate_id_unique_and_prefixed() -> None:
    reg = SubagentRegistry()
    ids = {reg.generate_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("sa-") for i in ids)


def test_registry_nonpositive_cap_falls_back_to_default() -> None:
    assert SubagentRegistry(max_resumable=0)._max == DEFAULT_MAX_RESUMABLE
    assert SubagentRegistry(max_resumable=-5)._max == DEFAULT_MAX_RESUMABLE


# --------------------------------------------------------------------------- #
# run_subagent_send                                                           #
# --------------------------------------------------------------------------- #
def test_send_resumes_appends_message_and_returns_footnote() -> None:
    reg = SubagentRegistry()
    ctx = _ctx("sa-abc", messages=[{"role": "user", "content": "first"}])
    reg.register(ctx)
    calls: list[ResumableContext] = []

    def _run_loop(context: ResumableContext) -> str:
        calls.append(context)
        return "resumed-result"

    result = run_subagent_send("sa-abc", "follow up", registry=reg, run_loop=_run_loop)

    assert calls == [ctx]
    assert ctx.messages[-1] == {"role": "user", "content": "follow up"}
    assert "resumed-result" in result
    assert "sa-abc" in result
    assert "subagent_send" in result
    assert reg.has("sa-abc")  # still resumable after a turn


def test_send_missing_id_returns_error_not_raise() -> None:
    reg = SubagentRegistry()
    result = run_subagent_send("sa-nope", "hi", registry=reg, run_loop=lambda _ctx: "x")
    assert result.startswith("Error:")
    assert "not found" in result


def test_send_empty_agent_id_returns_error() -> None:
    reg = SubagentRegistry()
    called = False

    def _run_loop(_ctx: ResumableContext) -> str:
        nonlocal called
        called = True
        return "x"

    result = run_subagent_send("   ", "hi", registry=reg, run_loop=_run_loop)
    assert result.startswith("Error:")
    assert not called


def test_send_empty_message_returns_error() -> None:
    reg = SubagentRegistry()
    reg.register(_ctx("sa-x"))
    result = run_subagent_send("sa-x", "  ", registry=reg, run_loop=lambda _ctx: "x")
    assert result.startswith("Error:")


def test_send_multi_turn_same_id_stays_resumable() -> None:
    reg = SubagentRegistry()
    ctx = _ctx("sa-multi", messages=[])
    reg.register(ctx)

    run_subagent_send("sa-multi", "turn 1", registry=reg, run_loop=lambda _c: "r1")
    second = run_subagent_send("sa-multi", "turn 2", registry=reg, run_loop=lambda _c: "r2")

    assert "r2" in second
    # Both user turns landed on the same live conversation.
    assert {"role": "user", "content": "turn 1"} in ctx.messages
    assert {"role": "user", "content": "turn 2"} in ctx.messages


# --------------------------------------------------------------------------- #
# run_subagent registration                                                   #
# --------------------------------------------------------------------------- #
def test_foreground_none_isolation_registers_context(monkeypatch) -> None:
    monkeypatch.setattr("bareagent.planning.subagent.agent_loop", lambda **_kw: "done")
    reg = SubagentRegistry()

    result = run_subagent(
        provider=object(),
        task="do the thing",
        tools=[{"name": "read_file"}],
        handlers={"read_file": object()},
        permission=PermissionGuard(PermissionMode.DEFAULT),
        registry=reg,
        agent_type="explore",
    )

    assert "done" in result
    match = _FOOTNOTE_ID.search(result)
    assert match is not None
    agent_id = match.group(1)
    ctx = reg.get(agent_id)
    assert ctx is not None
    assert ctx.max_turns == 50  # explore agent_type budget
    assert "do the thing" in str(ctx.messages)
    assert len(reg) == 1


def test_no_registry_does_not_register_and_has_no_footnote(monkeypatch) -> None:
    monkeypatch.setattr("bareagent.planning.subagent.agent_loop", lambda **_kw: "done")
    # registry omitted (default None) mirrors the nested-subagent path.
    result = run_subagent(
        provider=object(),
        task="x",
        tools=[],
        handlers={},
        permission=PermissionGuard(PermissionMode.DEFAULT),
    )
    assert result == "done"
    assert "resumable" not in result


def test_worktree_isolation_does_not_register(monkeypatch) -> None:
    monkeypatch.setattr("bareagent.planning.subagent.agent_loop", lambda **_kw: "done")
    # Force the fail-open "not a git repo" path so no real worktree is created.
    monkeypatch.setattr("bareagent.planning.subagent.is_git_repo", lambda _base: False)
    reg = SubagentRegistry()

    result = run_subagent(
        provider=object(),
        task="x",
        tools=[],
        handlers={},
        permission=PermissionGuard(PermissionMode.DEFAULT),
        registry=reg,
        isolation="worktree",
    )

    assert len(reg) == 0
    assert "resumable" not in result


# --------------------------------------------------------------------------- #
# isolation guarantees                                                        #
# --------------------------------------------------------------------------- #
def test_subagent_send_is_main_loop_only() -> None:
    assert "subagent_send" in MAIN_LOOP_ONLY_TOOLS


def test_subagent_send_stripped_for_every_agent_type() -> None:
    tools = [SUBAGENT_SEND_TOOL_SCHEMA, {"name": "read_file"}]
    for type_name in ("general-purpose", "explore", "plan", "code-review"):
        agent_type = resolve_agent_type(type_name)
        names = {t["name"] for t in filter_tools(tools, agent_type)}
        assert "subagent_send" not in names


def test_subagent_send_not_in_safe_tools() -> None:
    assert "subagent_send" not in PermissionGuard.SAFE_TOOLS
