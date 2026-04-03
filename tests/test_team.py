from __future__ import annotations

import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

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
    assert decode_protocol_content(tester_messages[0].content) == (Protocol.SHUTDOWN, "停止")


def test_autonomous_agent_replies_and_claims_ready_tasks(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path / ".mailbox")
    bus.ensure_mailbox("main")
    task_manager = TaskManager(tmp_path / ".tasks.json")
    task = task_manager.create("Review module", description="Inspect the new manager module")
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
    task = task_manager.create("Review module", description="Inspect the new manager module")
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


def test_autonomous_agent_respects_permission_guard_for_received_work(tmp_path: Path) -> None:
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
            "write_file": lambda file_path, content: handler_calls.append((file_path, content))
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
        messages = bus.receive(agent_name, since=cursor)
        if messages:
            cursor = messages[-1].timestamp
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
