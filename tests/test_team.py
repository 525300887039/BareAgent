from __future__ import annotations

import threading
import time
import types
from copy import deepcopy
from pathlib import Path

import pytest

from src.concurrency.background import BackgroundManager
from src.main import (
    MAIN_AGENT_NAME,
    TeamConfig,
    _drain_team_mailbox,
    _make_team_handlers,
    _parse_team_config,
)
from src.permission.guard import PermissionGuard, PermissionMode
from src.planning.tasks import TaskManager
from src.provider.base import BaseLLMProvider, LLMResponse, ToolCall
from src.team.autonomous import AutonomousAgent
from src.team.mailbox import Message, MessageBus
from src.team.manager import TeammateManager
from src.team.protocols import Protocol, ProtocolFSM, decode_protocol_content


class ReplayProvider(BaseLLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "kwargs": kwargs,
            }
        )
        text = self.responses.pop(0)
        return LLMResponse(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=2,
        )

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class SequenceProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "kwargs": kwargs,
            }
        )
        return self.responses.pop(0)

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def test_teammate_manager_persists_and_spawns(tmp_path: Path) -> None:
    config_file = tmp_path / ".team.json"
    manager = TeammateManager(config_file)

    manager.register(
        "code-reviewer",
        "code reviewer",
        "You review code changes.",
        provider_config={"name": "openai", "model": "gpt-test"},
    )

    reloaded = TeammateManager(config_file)
    teammate = reloaded.get("code-reviewer")
    spawned = reloaded.spawn(
        "code-reviewer",
        lambda provider_config: {
            "provider": provider_config["name"],
            "model": provider_config["model"],
        },
    )

    assert teammate.role == "code reviewer"
    assert [item.name for item in reloaded.list()] == ["code-reviewer"]
    assert spawned.system_prompt == "You review code changes."
    assert spawned.provider == {"provider": "openai", "model": "gpt-test"}


def test_message_bus_send_receive_and_find(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    bus.ensure_mailbox("reviewer")

    message_id = bus.send(
        Message(
            id="",
            from_agent="main",
            to_agent="reviewer",
            content="please review this patch",
            msg_type="request",
            timestamp="",
        )
    )

    inbox = bus.receive("reviewer")

    assert bus.list_agents() == ["main", "reviewer"]
    assert [message.id for message in inbox] == [message_id]
    assert inbox[0].content == "please review this patch"
    assert bus.find_message(message_id) is not None


def test_protocol_fsm_waits_for_response_and_broadcasts(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    bus.ensure_mailbox("reviewer")
    bus.ensure_mailbox("tester")
    main_fsm = ProtocolFSM(bus, "main")
    reviewer_fsm = ProtocolFSM(bus, "reviewer")

    request_id = main_fsm.request("reviewer", Protocol.PLAN_APPROVAL, "计划内容")

    def _respond() -> None:
        time.sleep(0.05)
        reviewer_fsm.respond(request_id, "批准")

    responder = threading.Thread(target=_respond, daemon=True)
    responder.start()
    response = main_fsm.wait_response(request_id, timeout=1)
    responder.join(timeout=1)

    assert response is not None
    assert response.in_reply_to == request_id
    assert decode_protocol_content(response.content) == (Protocol.PLAN_APPROVAL, "批准")

    message_ids = reviewer_fsm.broadcast(Protocol.SHUTDOWN, "停止")
    tester_messages = bus.receive("tester")

    assert len(message_ids) == 2
    assert decode_protocol_content(tester_messages[0].content) == (
        Protocol.SHUTDOWN,
        "停止",
    )


def test_autonomous_agent_replies_and_claims_ready_tasks(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    task_manager = TaskManager(tmp_path / ".tasks.json")
    task = task_manager.create(
        "Review module", description="Inspect the new manager module"
    )
    provider = ReplayProvider(["Message handled.", "Task completed."])
    agent = AutonomousAgent(
        name="code-reviewer",
        provider=provider,
        tools=[],
        handlers={},
        bus=bus,
        task_manager=task_manager,
        system_prompt="You are a code reviewer.",
        poll_interval=0.01,
    )
    request_id = bus.send(
        Message(
            id="",
            from_agent="main",
            to_agent="code-reviewer",
            content="Please review src/main.py",
            msg_type="request",
            timestamp="",
        )
    )

    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    response = _wait_for_message(
        bus,
        agent_name="main",
        predicate=lambda message: message.in_reply_to == request_id,
    )
    _wait_for(lambda: task_manager.get(task.id).status == "done")
    ProtocolFSM(bus, "main").broadcast(Protocol.SHUTDOWN, "stop")
    thread.join(timeout=1)

    assert response is not None
    assert response.from_agent == "code-reviewer"
    assert response.content == "Message handled."
    assert task_manager.get(task.id).status == "done"
    assert not thread.is_alive()
    assert len(provider.calls) >= 2


def test_autonomous_agent_ignores_stale_shutdown_messages(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    task_manager = TaskManager(tmp_path / ".tasks.json")
    task = task_manager.create(
        "Review module", description="Inspect the new manager module"
    )
    bus.send(
        Message(
            id="",
            from_agent="main",
            to_agent="code-reviewer",
            content='{"protocol": "shutdown", "content": "old shutdown"}',
            msg_type="broadcast",
            timestamp="",
        )
    )

    provider = ReplayProvider(["Task completed."])
    agent = AutonomousAgent(
        name="code-reviewer",
        provider=provider,
        tools=[],
        handlers={},
        bus=bus,
        task_manager=task_manager,
        system_prompt="You are a code reviewer.",
        poll_interval=0.01,
    )
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    _wait_for(lambda: task_manager.get(task.id).status == "done")
    assert thread.is_alive()

    ProtocolFSM(bus, "main").broadcast(Protocol.SHUTDOWN, "stop")
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(provider.calls) == 1


def test_autonomous_agent_respects_permission_guard_for_received_work(
    tmp_path: Path,
) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    provider = SequenceProvider(
        [
            LLMResponse(
                text="Need to write a file first.",
                tool_calls=[
                    ToolCall(
                        id="toolu_write",
                        name="write_file",
                        input={"file_path": "out.txt", "content": "denied"},
                    )
                ],
                stop_reason="tool_use",
                input_tokens=10,
                output_tokens=5,
            ),
            LLMResponse(
                text="Write denied, stopping.",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=8,
                output_tokens=3,
            ),
        ]
    )
    handler_calls: list[tuple[str, str]] = []
    agent = AutonomousAgent(
        name="code-reviewer",
        provider=provider,
        tools=[],
        handlers={
            "write_file": lambda file_path, content: handler_calls.append(
                (file_path, content)
            )
        },
        bus=bus,
        task_manager=None,
        permission=PermissionGuard(PermissionMode.PLAN),
        system_prompt="You are a code reviewer.",
        poll_interval=0.01,
    )
    request_id = bus.send(
        Message(
            id="",
            from_agent="main",
            to_agent="code-reviewer",
            content="Please modify out.txt",
            msg_type="request",
            timestamp="",
        )
    )
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    response = _wait_for_message(
        bus,
        agent_name="main",
        predicate=lambda message: message.in_reply_to == request_id,
    )
    ProtocolFSM(bus, "main").broadcast(Protocol.SHUTDOWN, "stop")
    thread.join(timeout=1)

    assert response is not None
    assert response.content == "Write denied, stopping."
    assert handler_calls == []
    assert len(provider.calls) == 2
    tool_result_blocks = provider.calls[1]["messages"][-1]["content"]  # type: ignore[index]
    assert tool_result_blocks[0]["is_error"] is True  # type: ignore[index]
    assert tool_result_blocks[0]["content"] == "User denied."  # type: ignore[index]


def test_task_manager_expected_status_supports_optimistic_claim(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / ".tasks.json")
    task = manager.create("A")
    manager.update(task.id, status="in_progress")

    with pytest.raises(ValueError, match="expected pending"):
        manager.update(task.id, status="in_progress", expected_status="pending")


def _wait_for_message(
    bus: MessageBus,
    *,
    agent_name: str,
    predicate,
    timeout: float = 2,
):
    deadline = time.time() + timeout
    cursor: str | None = None
    while time.time() < deadline:
        messages = bus.receive(agent_name, since_id=cursor)
        if messages:
            cursor = messages[-1].id
        for message in messages:
            if predicate(message):
                return message
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for mailbox message")


def _wait_for(predicate, timeout: float = 2) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


# --- task 06-06-team-subsystem-completion ---------------------------------


class _FailingProvider(BaseLLMProvider):
    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = messages, tools, kwargs
        raise RuntimeError("boom")

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


class _FakeBg:
    """Minimal stand-in for BackgroundManager.is_running / submit."""

    def __init__(self, running: set[str] | None = None) -> None:
        self.running: set[str] = set(running or ())
        self.submitted: list[str] = []

    def is_running(self, task_id: str) -> bool:
        return task_id in self.running

    def submit(self, task_id: str, fn, *args):
        self.submitted.append(task_id)
        return task_id


class _FakeConsole:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def print_status(self, message: str) -> None:
        self.statuses.append(message)


def _build_team_handlers(
    tmp_path: Path,
    *,
    bus: MessageBus,
    teammate_manager: TeammateManager,
    bg: _FakeBg,
    spawned: dict | None = None,
    response_timeout: float = 0.3,
    runtime_id: str = "sess1",
    agent_name: str = MAIN_AGENT_NAME,
) -> dict:
    config = types.SimpleNamespace(
        team=TeamConfig(poll_interval=0.01, response_timeout=response_timeout),
        provider=types.SimpleNamespace(name="anthropic", model="m"),
    )
    return _make_team_handlers(
        config=config,  # type: ignore[arg-type]
        workspace_path=tmp_path,
        todo_manager=None,  # type: ignore[arg-type]  # only used by team_spawn
        task_manager=None,
        skill_loader=None,  # type: ignore[arg-type]  # only used by team_spawn
        permission=PermissionGuard(PermissionMode.DEFAULT),
        bg_manager=bg,  # type: ignore[arg-type]
        tools=[],
        runtime_id=runtime_id,
        teammate_manager=teammate_manager,
        message_bus=bus,
        spawned_agents=spawned if spawned is not None else {},
        agent_name=agent_name,
    )


def test_background_manager_is_running(tmp_path: Path) -> None:
    bg = BackgroundManager()
    assert bg.is_running("never-submitted") is False

    started = threading.Event()
    release = threading.Event()

    def _work() -> None:
        started.set()
        release.wait(timeout=2)

    bg.submit("job1", _work)
    assert started.wait(timeout=2)
    assert bg.is_running("job1") is True

    release.set()
    _wait_for(lambda: not bg.is_running("job1"))
    assert bg.is_running("job1") is False


def test_mailbox_mark_delivered_dedup(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    assert bus.was_delivered("m1") is False
    bus.mark_delivered("m1")
    assert bus.was_delivered("m1") is True
    # Empty / whitespace ids are a no-op (never recorded, never matched).
    bus.mark_delivered("   ")
    assert bus.was_delivered("") is False


def test_parse_team_config_defaults_values_and_fallbacks() -> None:
    defaults = _parse_team_config({})
    assert defaults.poll_interval == 1.0
    assert defaults.response_timeout == 60.0

    custom = _parse_team_config({"poll_interval": 0.5, "response_timeout": 30})
    assert custom.poll_interval == 0.5
    assert custom.response_timeout == 30.0

    # Malformed and non-positive values fall back to defaults.
    bad = _parse_team_config({"poll_interval": "nope", "response_timeout": 0})
    assert bad.poll_interval == 1.0
    assert bad.response_timeout == 60.0


def test_autonomous_agent_isolates_request_failure(tmp_path: Path) -> None:
    """A failing request must not kill the daemon; it replies with the error."""
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    agent = AutonomousAgent(
        name="reviewer",
        provider=_FailingProvider(),
        tools=[],
        handlers={},
        bus=bus,
        task_manager=None,
    )
    request_id = bus.send(
        Message(
            id="",
            from_agent="main",
            to_agent="reviewer",
            content="do the thing",
            msg_type="request",
            timestamp="",
        )
    )
    incoming = bus.receive("reviewer")
    # Direct call (no thread): _handle_messages must swallow the error.
    agent._handle_messages(incoming)

    replies = bus.receive("main")
    assert len(replies) == 1
    assert replies[0].in_reply_to == request_id
    assert "[error]" in replies[0].content


def test_team_send_blocks_and_returns_reply(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox(MAIN_AGENT_NAME)
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    teammate_manager.register("reviewer", "reviewer", "You review.")
    bg = _FakeBg(running={"team:sess1:reviewer"})
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg
    )

    stop = threading.Event()

    def _responder() -> None:
        fsm = ProtocolFSM(bus, "reviewer")
        cursor: str | None = None
        while not stop.is_set():
            msgs = bus.receive("reviewer", since_id=cursor)
            if msgs:
                cursor = msgs[-1].id
            for message in msgs:
                if message.msg_type == "request":
                    fsm.respond(message.id, f"done:{message.content}")
            time.sleep(0.005)

    thread = threading.Thread(target=_responder, daemon=True)
    thread.start()
    try:
        result = handlers["team_send"]("reviewer", "hello")
    finally:
        stop.set()
        thread.join(timeout=1)

    assert result == "Reply from reviewer: done:hello"
    # The consumed reply is marked delivered so the drain won't re-surface it.
    main_inbox = bus.receive(MAIN_AGENT_NAME)
    assert any(bus.was_delivered(m.id) for m in main_inbox)


def test_team_send_skips_blocking_when_not_running(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    teammate_manager.register("reviewer", "reviewer", "You review.")
    bg = _FakeBg(running=set())  # not running
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg
    )

    start = time.time()
    result = handlers["team_send"]("reviewer", "hello")
    elapsed = time.time() - start

    assert "not running" in result
    assert elapsed < 0.2  # returned immediately, did not wait out the timeout


def test_team_send_to_main_returns_immediately(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    bg = _FakeBg()
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg
    )

    result = handlers["team_send"](MAIN_AGENT_NAME, "note")
    assert result.startswith("Sent message")
    assert MAIN_AGENT_NAME in result


def test_team_send_times_out_without_reply(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    teammate_manager.register("reviewer", "reviewer", "You review.")
    bg = _FakeBg(running={"team:sess1:reviewer"})
    handlers = _build_team_handlers(
        tmp_path,
        bus=bus,
        teammate_manager=teammate_manager,
        bg=bg,
        response_timeout=0.1,
    )

    result = handlers["team_send"]("reviewer", "hello")
    assert "no reply within" in result


def test_team_shutdown_signals_running_teammate(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    teammate_manager.register("reviewer", "reviewer", "You review.")
    spawned: dict = {"reviewer": object()}
    bg = _FakeBg(running={"team:sess1:reviewer"})
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg, spawned=spawned
    )

    result = handlers["team_shutdown"]("reviewer")
    assert "shutdown" in result.lower()
    assert "reviewer" not in spawned

    inbox = bus.receive("reviewer")
    assert any(
        decode_protocol_content(m.content)[0] == Protocol.SHUTDOWN for m in inbox
    )


def test_team_shutdown_when_not_running(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    bg = _FakeBg(running=set())
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg
    )

    result = handlers["team_shutdown"]("reviewer")
    assert "not running" in result


def test_team_list_reflects_real_liveness(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    teammate_manager = TeammateManager.create_empty(tmp_path / ".team.json")
    teammate_manager.register("alive", "r", "p")
    teammate_manager.register("dead", "r", "p")
    bg = _FakeBg(running={"team:sess1:alive"})
    handlers = _build_team_handlers(
        tmp_path, bus=bus, teammate_manager=teammate_manager, bg=bg
    )

    listed = {item["name"]: item["running"] for item in handlers["team_list"]()}
    assert listed == {"alive": True, "dead": False}


def test_drain_team_mailbox_skips_delivered_and_collects_sink(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox(MAIN_AGENT_NAME)
    delivered_id = bus.send(
        Message(
            id="",
            from_agent="reviewer",
            to_agent=MAIN_AGENT_NAME,
            content="already returned to the LLM",
            msg_type="response",
            timestamp="",
        )
    )
    bus.mark_delivered(delivered_id)
    bus.send(
        Message(
            id="",
            from_agent="reviewer",
            to_agent=MAIN_AGENT_NAME,
            content="unsolicited late note",
            msg_type="response",
            timestamp="",
        )
    )

    console = _FakeConsole()
    sink: list[str] = []
    cursor = _drain_team_mailbox(
        console,  # type: ignore[arg-type]
        message_bus=bus,
        since=None,
        sink=sink,
    )

    # The delivered message is skipped; only the unsolicited one is surfaced.
    assert len(sink) == 1
    assert "unsolicited late note" in sink[0]
    assert all("already returned" not in status for status in console.statuses)
    assert cursor is not None


def test_message_bus_receive_does_not_lose_same_timestamp_messages(
    tmp_path: Path,
) -> None:
    """Bug #15: messages with identical timestamps should not be skipped."""
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("agent")

    fixed_ts = "2025-01-01T00:00:00+00:00"
    msg1_id = bus.send(
        Message(
            id="",
            from_agent="a",
            to_agent="agent",
            content="first",
            msg_type="request",
            timestamp=fixed_ts,
        )
    )
    msg2_id = bus.send(
        Message(
            id="",
            from_agent="b",
            to_agent="agent",
            content="second",
            msg_type="request",
            timestamp=fixed_ts,
        )
    )

    # Read using msg1 as cursor — should still get msg2
    messages = bus.receive("agent", since_id=msg1_id)
    assert len(messages) == 1
    assert messages[0].id == msg2_id
    assert messages[0].content == "second"
