from __future__ import annotations

import time
from typing import Any

from src.core.loop import agent_loop, LLMCallError
from src.planning.tasks import Task, TaskManager
from src.team.mailbox import Message, MessageBus
from src.team.protocols import Protocol, ProtocolFSM, decode_protocol_content


class AutonomousAgent:
    """Daemon-friendly idle-poll-claim-work loop for teammate agents."""

    def __init__(
        self,
        name: str,
        provider: Any,
        tools: list[dict[str, Any]],
        handlers: dict[str, Any],
        bus: MessageBus,
        task_manager: TaskManager | None,
        *,
        permission: Any = None,
        system_prompt: str = "",
        poll_interval: float = 5.0,
    ) -> None:
        self.name = name
        self.provider = provider
        self.tools = tools
        self.handlers = handlers
        self.bus = bus
        self.task_manager = task_manager
        self.permission = permission
        self.system_prompt = system_prompt.strip()
        self.poll_interval = poll_interval
        self._shutdown = False
        self.bus.ensure_mailbox(name)
        self._last_seen_id: str | None = self.bus.latest_message_id(name)
        self._protocol = ProtocolFSM(bus, agent_name=name)

    def run(self) -> str:
        while not self._shutdown:
            incoming = self.bus.receive(self.name, since_id=self._last_seen_id)
            if incoming:
                self._last_seen_id = incoming[-1].id
                self._handle_messages(incoming)
                continue

            if self.task_manager is not None:
                ready_tasks = self.task_manager.get_ready_tasks()
                for task in ready_tasks:
                    claimed_task = self._claim_task(task)
                    if claimed_task is None:
                        continue
                    self._execute_task(claimed_task)
                    break
                else:
                    time.sleep(self.poll_interval)
                continue

            time.sleep(self.poll_interval)

        return f"{self.name} stopped"

    def _handle_messages(self, messages: list[Message]) -> None:
        for message in messages:
            protocol, content = decode_protocol_content(message.content)
            if protocol == Protocol.SHUTDOWN:
                self._shutdown = True
                continue

            if message.msg_type != "request":
                continue

            response_text = self._run_prompt(
                self._build_incoming_prompt(content, protocol=protocol)
            )
            self._protocol.respond(message.id, response_text)

    def _claim_task(self, task: Task) -> Task | None:
        if self.task_manager is None:
            return None

        try:
            return self.task_manager.update(
                task.id,
                status="in_progress",
                expected_status="pending",
            )
        except ValueError:
            return None

    def _execute_task(self, task: Task) -> None:
        if self.task_manager is None:
            return

        try:
            self._run_prompt(self._build_task_prompt(task))
        except LLMCallError:
            self.task_manager.update(task.id, status="failed")
            return
        except Exception:
            self.task_manager.update(task.id, status="failed")
            return

        self.task_manager.update(task.id, status="done")

    def _run_prompt(self, prompt: str) -> str:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        return agent_loop(
            provider=self.provider,
            messages=messages,
            tools=self.tools,
            handlers=self.handlers,
            permission=self.permission,
            compact_fn=lambda _messages: None,
        )

    def _build_incoming_prompt(
        self,
        content: str,
        *,
        protocol: Protocol | None,
    ) -> str:
        if protocol == Protocol.PLAN_APPROVAL:
            return (
                "请审阅下面的计划，判断是否应批准，并给出简洁理由。\n\n"
                + content
            )
        return content

    def _build_task_prompt(self, task: Task) -> str:
        lines = [
            f"你是队友 {self.name}，请完成下面的任务。",
            f"任务标题: {task.title}",
        ]
        if task.description:
            lines.append(f"任务描述: {task.description}")
        lines.append("完成后给出简洁结果。")
        return "\n".join(lines)
