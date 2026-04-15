from __future__ import annotations

import time

from src.concurrency.background import BackgroundManager
from src.permission.guard import PermissionGuard, PermissionMode
from src.planning.subagent import run_subagent
from src.provider.base import BaseLLMProvider, LLMResponse


class RecordingProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        _ = tools, kwargs
        self.messages = [dict(message) for message in messages]
        return LLMResponse(
            text="subagent done",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=0,
            output_tokens=0,
        )

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def test_subagent_depth_limit() -> None:
    result = run_subagent(
        provider=RecordingProvider(),
        task="test",
        tools=[],
        handlers={},
        permission=None,
        current_depth=4,
        max_depth=3,
    )

    assert "depth" in result.lower()
    assert "exceeds" in result.lower() or "refused" in result.lower()


def test_run_subagent_applies_agent_type_filters_and_max_turns(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_agent_loop(
        *,
        provider,
        messages,
        tools,
        handlers,
        permission,
        compact_fn,
        bg_manager,
        max_iterations,
        **kwargs,
    ) -> str:
        _ = provider, compact_fn, kwargs
        captured["messages"] = messages
        captured["tools"] = [tool["name"] for tool in tools]
        captured["handlers"] = sorted(handlers)
        captured["permission"] = permission
        captured["bg_manager"] = bg_manager
        captured["max_iterations"] = max_iterations
        return "planned"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)

    result = run_subagent(
        provider=RecordingProvider(),
        task="Inspect the repo",
        tools=[
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "bash"},
            {"name": "subagent"},
        ],
        handlers={
            "read_file": object(),
            "write_file": object(),
            "bash": object(),
            "subagent": object(),
        },
        permission=PermissionGuard(PermissionMode.DEFAULT),
        system_prompt="Parent instructions",
        agent_type="explore",
    )

    assert result == "planned"
    assert captured["tools"] == ["read_file"]
    assert captured["handlers"] == ["read_file"]
    assert captured["max_iterations"] == 50
    assert captured["bg_manager"] is None
    assert "Parent instructions" in str(captured["messages"])


def test_run_subagent_unknown_type_falls_back_to_configured_default(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_agent_loop(*, tools, max_iterations, **kwargs) -> str:
        _ = kwargs
        captured["tools"] = [tool["name"] for tool in tools]
        captured["max_iterations"] = max_iterations
        return "planned"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)

    result = run_subagent(
        provider=RecordingProvider(),
        task="Plan the work",
        tools=[
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "subagent"},
        ],
        handlers={
            "read_file": object(),
            "write_file": object(),
            "subagent": object(),
        },
        permission=PermissionGuard(PermissionMode.DEFAULT),
        agent_type="missing",
        default_agent_type="plan",
    )

    assert result == "planned"
    assert captured["tools"] == ["read_file"]
    assert captured["max_iterations"] == 50


def test_run_subagent_background_submission() -> None:
    manager = BackgroundManager()
    provider = RecordingProvider()

    submission = run_subagent(
        provider=provider,
        task="Inspect the repo",
        tools=[],
        handlers={},
        permission=PermissionGuard(PermissionMode.DEFAULT),
        bg_manager=manager,
        run_in_background=True,
    )

    deadline = time.time() + 2
    notifications: list[dict[str, object]] = []
    while time.time() < deadline:
        notifications = manager.drain_notifications()
        if notifications:
            break
        time.sleep(0.01)

    assert submission.startswith("Subagent subagent-")
    assert notifications == [
        {
            "task_id": notifications[0]["task_id"],
            "status": "done",
            "result": "subagent done",
        }
    ]


def test_run_subagent_background_worker_does_not_drain_shared_notifications(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_agent_loop(
        *,
        bg_manager,
        **kwargs,
    ) -> str:
        _ = kwargs
        captured["bg_manager"] = bg_manager
        return "subagent done"

    monkeypatch.setattr("src.planning.subagent.agent_loop", _fake_agent_loop)
    manager = BackgroundManager()

    submission = run_subagent(
        provider=RecordingProvider(),
        task="Inspect the repo",
        tools=[],
        handlers={},
        permission=PermissionGuard(PermissionMode.DEFAULT),
        bg_manager=manager,
        run_in_background=True,
    )

    deadline = time.time() + 2
    notifications: list[dict[str, object]] = []
    while time.time() < deadline:
        notifications = manager.drain_notifications()
        if notifications:
            break
        time.sleep(0.01)

    assert submission.startswith("Subagent subagent-")
    assert captured["bg_manager"] is None
    assert notifications[0]["status"] == "done"
