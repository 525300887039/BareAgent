from __future__ import annotations

import json
import time
from enum import Enum

from src.team.mailbox import Message, MessageBus


class Protocol(Enum):
    PLAN_APPROVAL = "plan_approval"
    SHUTDOWN = "shutdown"


class ProtocolFSM:
    """Simple polling request-response helper built on MessageBus."""

    def __init__(self, bus: MessageBus, agent_name: str) -> None:
        self.bus = bus
        self.agent_name = agent_name
        self.bus.ensure_mailbox(agent_name)

    def request(self, to: str, protocol: Protocol, content: str) -> str:
        return self.bus.send(
            Message(
                id="",
                from_agent=self.agent_name,
                to_agent=to,
                content=encode_protocol_content(protocol, content),
                msg_type="request",
                timestamp="",
            )
        )

    def wait_response(self, msg_id: str, timeout: float = 60) -> Message | None:
        deadline = time.monotonic() + timeout
        cursor: str | None = None

        while time.monotonic() < deadline:
            messages = self.bus.receive(self.agent_name, since=cursor)
            if messages:
                cursor = messages[-1].timestamp
            for message in messages:
                if message.msg_type != "response":
                    continue
                if message.in_reply_to == msg_id:
                    return message
            time.sleep(0.1)

        return None

    def respond(self, in_reply_to: str, content: str) -> str:
        request_message = self.bus.find_message(in_reply_to)
        if request_message is None:
            raise ValueError(f"Unknown request message id: {in_reply_to}")

        protocol, _ = decode_protocol_content(request_message.content)
        response_content = (
            encode_protocol_content(protocol, content)
            if protocol is not None
            else content
        )
        return self.bus.send(
            Message(
                id="",
                from_agent=self.agent_name,
                to_agent=request_message.from_agent,
                content=response_content,
                msg_type="response",
                timestamp="",
                in_reply_to=in_reply_to,
            )
        )

    def broadcast(self, protocol: Protocol, content: str) -> list[str]:
        recipients = [
            agent_name
            for agent_name in self.bus.list_agents()
            if agent_name != self.agent_name
        ]
        message_ids: list[str] = []
        for recipient in recipients:
            message_ids.append(
                self.bus.send(
                    Message(
                        id="",
                        from_agent=self.agent_name,
                        to_agent=recipient,
                        content=encode_protocol_content(protocol, content),
                        msg_type="broadcast",
                        timestamp="",
                    )
                )
            )
        return message_ids


def encode_protocol_content(protocol: Protocol, content: str) -> str:
    return json.dumps(
        {
            "protocol": protocol.value,
            "content": content,
        },
        ensure_ascii=False,
    )


def decode_protocol_content(content: str) -> tuple[Protocol | None, str]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None, str(content)

    if not isinstance(payload, dict):
        return None, str(content)

    protocol_name = payload.get("protocol")
    body = payload.get("content", "")
    if not isinstance(protocol_name, str):
        return None, str(content)

    try:
        protocol = Protocol(protocol_name)
    except ValueError:
        return None, str(content)
    return protocol, str(body)
