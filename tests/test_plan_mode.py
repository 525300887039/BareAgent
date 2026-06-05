"""Tests for the plan-mode workflow (exit_plan_mode tool + approval + injection).

Covers three layers:
- ``run_exit_plan_mode`` handler: pure decision -> LLM-result-string mapping.
- ``_make_plan_approval`` callback: three-way UI prompt + permission-mode flip.
- ``_refresh_plan_directive`` / ``_build_loop_compact``: per-iteration injection.
Plus the isolation guarantees (sub-agents never see the tool; PLAN allows it).
"""

import src.main as main_module
from src.core.handlers.plan import (
    EXIT_PLAN_MODE_TOOL_SCHEMA,
    PlanDecision,
    run_exit_plan_mode,
)
from src.main import (
    _build_loop_compact,
    _install_plan_handler,
    _make_plan_approval,
    _refresh_plan_directive,
)
from src.permission.guard import PermissionGuard, PermissionMode
from src.planning.agent_types import BUILTIN_AGENT_TYPES, filter_tools


class _FakeConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.assistant: list[str] = []

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)

    def print_assistant(self, text: str) -> None:
        self.assistant.append(text)


def _fake_todo():
    class _T:
        def get_nag_reminder(self) -> str:
            return ""

    return _T()


# --------------------------------------------------------------------------- #
# Handler: run_exit_plan_mode (pure mapping)
# --------------------------------------------------------------------------- #


def test_run_exit_plan_mode_rejects_empty_plan() -> None:
    def _never(_plan: str) -> PlanDecision:  # pragma: no cover - must not run
        raise AssertionError("approve_fn must not be called for an empty plan")

    result = run_exit_plan_mode(plan="   ", approve_fn=_never)
    assert result.startswith("Error:")
    assert "non-empty" in result


def test_run_exit_plan_mode_passes_plan_to_approve_fn() -> None:
    seen: list[str] = []

    def _capture(plan: str) -> PlanDecision:
        seen.append(plan)
        return PlanDecision("reject")

    run_exit_plan_mode(plan="my plan", approve_fn=_capture)
    assert seen == ["my plan"]


def test_run_exit_plan_mode_approve_default_message() -> None:
    result = run_exit_plan_mode(plan="p", approve_fn=lambda _p: PlanDecision("approve-default"))
    assert "DEFAULT" in result
    assert "Proceed" in result


def test_run_exit_plan_mode_approve_auto_message() -> None:
    result = run_exit_plan_mode(plan="p", approve_fn=lambda _p: PlanDecision("approve-auto"))
    assert "AUTO" in result
    assert "Proceed" in result


def test_run_exit_plan_mode_reject_with_reason_feeds_reason_back() -> None:
    result = run_exit_plan_mode(
        plan="p",
        approve_fn=lambda _p: PlanDecision("reject", "touch only the API layer"),
    )
    assert "touch only the API layer" in result
    assert "still in plan mode" in result


def test_run_exit_plan_mode_reject_without_reason_is_generic() -> None:
    result = run_exit_plan_mode(plan="p", approve_fn=lambda _p: PlanDecision("reject"))
    assert "still in plan mode" in result
    assert "Reason:" not in result


def test_run_exit_plan_mode_noop_when_not_in_plan() -> None:
    result = run_exit_plan_mode(plan="p", approve_fn=lambda _p: PlanDecision("noop"))
    assert result.startswith("Error:")
    assert "plan mode" in result


def test_run_exit_plan_mode_unavailable_stays_in_plan() -> None:
    result = run_exit_plan_mode(plan="p", approve_fn=lambda _p: PlanDecision("unavailable"))
    assert "non-interactive" in result
    assert "plan mode" in result


# --------------------------------------------------------------------------- #
# Approval callback: _make_plan_approval (UI + mode flip)
# --------------------------------------------------------------------------- #


def _approval(monkeypatch, *, mode=PermissionMode.PLAN, tty=True, inputs=()):
    permission = PermissionGuard(mode)
    console = _FakeConsole()
    feed = iter(inputs)
    monkeypatch.setattr(main_module.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(main_module, "_read_stdio_input", lambda: next(feed))
    approve = _make_plan_approval(permission, console)
    return permission, console, approve


def test_plan_approval_choice_1_switches_to_default(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, inputs=["1"])
    decision = approve("the plan")
    assert decision.outcome == "approve-default"
    assert permission.mode == PermissionMode.DEFAULT


def test_plan_approval_choice_2_switches_to_auto(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, inputs=["2"])
    decision = approve("the plan")
    assert decision.outcome == "approve-auto"
    assert permission.mode == PermissionMode.AUTO


def test_plan_approval_choice_3_rejects_with_reason_and_stays_plan(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, inputs=["3", "too risky"])
    decision = approve("the plan")
    assert decision.outcome == "reject"
    assert decision.reason == "too risky"
    assert permission.mode == PermissionMode.PLAN


def test_plan_approval_unknown_choice_rejects(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, inputs=["9", ""])
    decision = approve("the plan")
    assert decision.outcome == "reject"
    assert permission.mode == PermissionMode.PLAN


def test_plan_approval_noop_when_not_in_plan_mode(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, mode=PermissionMode.DEFAULT, inputs=[])
    decision = approve("the plan")
    assert decision.outcome == "noop"
    assert permission.mode == PermissionMode.DEFAULT


def test_plan_approval_unavailable_when_non_interactive(monkeypatch) -> None:
    permission, _console, approve = _approval(monkeypatch, tty=False, inputs=[])
    decision = approve("the plan")
    assert decision.outcome == "unavailable"
    assert permission.mode == PermissionMode.PLAN


def test_plan_approval_unavailable_when_fail_closed(monkeypatch) -> None:
    # A fail-closed guard must never have its mode elevated via plan approval,
    # even with a tty present (error-handling.md: never approve when fail_closed).
    permission = PermissionGuard(PermissionMode.PLAN, fail_closed=True)
    console = _FakeConsole()
    monkeypatch.setattr(main_module.sys.stdin, "isatty", lambda: True)
    approve = _make_plan_approval(permission, console)

    decision = approve("the plan")
    assert decision.outcome == "unavailable"
    assert permission.mode == PermissionMode.PLAN


def test_plan_approval_eof_during_choice_rejects(monkeypatch) -> None:
    permission = PermissionGuard(PermissionMode.PLAN)
    console = _FakeConsole()

    def _raise_eof() -> str:
        raise EOFError

    monkeypatch.setattr(main_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main_module, "_read_stdio_input", _raise_eof)
    approve = _make_plan_approval(permission, console)

    decision = approve("the plan")
    assert decision.outcome == "reject"
    assert permission.mode == PermissionMode.PLAN


def test_plan_approval_renders_plan(monkeypatch) -> None:
    _permission, console, approve = _approval(monkeypatch, inputs=["1"])
    approve("RENDER ME")
    assert "RENDER ME" in console.assistant


# --------------------------------------------------------------------------- #
# Directive injection: _refresh_plan_directive / _build_loop_compact
# --------------------------------------------------------------------------- #


def _directive_blocks(messages: list[dict]) -> list[dict]:
    return [
        m
        for m in messages
        if m.get("role") == "system"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(main_module._PLAN_DIRECTIVE_PREFIX)
    ]


def test_refresh_plan_directive_injects_in_plan_mode() -> None:
    messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "do X"},
    ]
    _refresh_plan_directive(messages, PermissionGuard(PermissionMode.PLAN))

    blocks = _directive_blocks(messages)
    assert len(blocks) == 1
    # Injected immediately after the last genuine user message.
    assert messages[-1] is blocks[0]


def test_refresh_plan_directive_absent_outside_plan_mode() -> None:
    messages = [
        {"role": "user", "content": "do X"},
    ]
    _refresh_plan_directive(messages, PermissionGuard(PermissionMode.DEFAULT))
    assert _directive_blocks(messages) == []


def test_refresh_plan_directive_is_idempotent() -> None:
    permission = PermissionGuard(PermissionMode.PLAN)
    messages = [{"role": "user", "content": "do X"}]
    _refresh_plan_directive(messages, permission)
    _refresh_plan_directive(messages, permission)
    assert len(_directive_blocks(messages)) == 1


def test_refresh_plan_directive_removed_after_mode_flip() -> None:
    permission = PermissionGuard(PermissionMode.PLAN)
    messages = [{"role": "user", "content": "do X"}]
    _refresh_plan_directive(messages, permission)
    assert len(_directive_blocks(messages)) == 1

    # Mid-loop approval flips the mode; the next refresh must strip the block.
    permission.mode = PermissionMode.DEFAULT
    _refresh_plan_directive(messages, permission)
    assert _directive_blocks(messages) == []


def test_build_loop_compact_injects_plan_directive_when_permission_given() -> None:
    permission = PermissionGuard(PermissionMode.PLAN)
    compact = _build_loop_compact(
        lambda _messages, force=False: None,
        _fake_todo(),
        permission=permission,
    )
    messages = [{"role": "user", "content": "do X"}]
    compact(messages)
    assert len(_directive_blocks(messages)) == 1


def test_build_loop_compact_without_permission_does_not_inject() -> None:
    compact = _build_loop_compact(
        lambda _messages, force=False: None,
        _fake_todo(),
    )
    messages = [{"role": "user", "content": "do X"}]
    compact(messages)
    assert _directive_blocks(messages) == []


# --------------------------------------------------------------------------- #
# Isolation + permission guarantees
# --------------------------------------------------------------------------- #


def test_exit_plan_mode_stripped_for_every_builtin_subagent_type() -> None:
    tools = [{"name": "exit_plan_mode"}, {"name": "read_file"}]
    for agent_type in BUILTIN_AGENT_TYPES.values():
        kept = {t["name"] for t in filter_tools(tools, agent_type)}
        assert "exit_plan_mode" not in kept, agent_type.name


def test_exit_plan_mode_is_a_safe_tool() -> None:
    # Must be SAFE so PLAN mode (which blocks every non-SAFE tool) does not
    # block the one tool that leaves PLAN mode.
    assert "exit_plan_mode" in PermissionGuard.SAFE_TOOLS


def test_plan_mode_does_not_require_confirmation_for_exit_plan_mode() -> None:
    guard = PermissionGuard(PermissionMode.PLAN)
    assert guard.requires_confirm("exit_plan_mode", {"plan": "p"}) is False


def test_exit_plan_mode_schema_shape() -> None:
    assert EXIT_PLAN_MODE_TOOL_SCHEMA["name"] == "exit_plan_mode"
    assert "plan" in EXIT_PLAN_MODE_TOOL_SCHEMA["parameters"]["properties"]


def test_install_plan_handler_registers_callable_routed_to_approve_fn() -> None:
    # Session switches (/new, /resume, ...) rebuild ``handlers`` without the
    # main-loop-only handler; _install_plan_handler must put it back, wired to
    # the given approval callback (invoked as handlers["exit_plan_mode"](**input)).
    seen: list[str] = []

    def _approve(plan: str) -> PlanDecision:
        seen.append(plan)
        return PlanDecision("approve-default")

    handlers: dict = {}
    _install_plan_handler(handlers, _approve)

    assert "exit_plan_mode" in handlers
    result = handlers["exit_plan_mode"](plan="the plan")
    assert seen == ["the plan"]
    assert "DEFAULT" in result
