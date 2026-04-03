from __future__ import annotations

import json
import secrets
import string
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MESSAGE_ID_ALPHABET = string.ascii_letters + string.digits


@dataclass(slots=True)
class Message:
    id: str
    from_agent: str
    to_agent: str
    content: str
    msg_type: str
    timestamp: str
    in_reply_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Message":
        return cls(
            id=str(payload.get("id", "")),
            from_agent=str(payload.get("from_agent", "")),
            to_agent=str(payload.get("to_agent", "")),
            content=str(payload.get("content", "")),
            msg_type=str(payload.get("msg_type", "")),
            timestamp=str(payload.get("timestamp", "")),
            in_reply_to=optional_string(payload.get("in_reply_to")),
        )


class MessageBus:
    """Append-only JSONL mailboxes, one file per agent."""

    def __init__(self, mailbox_dir: str | Path = ".mailbox") -> None:
        self.mailbox_dir = Path(mailbox_dir)
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._message_index: OrderedDict[str, tuple[str, Message]] = OrderedDict()
        self._index_lock = threading.Lock()
        self._max_index_size = 10000
        self._conds: dict[str, threading.Condition] = {}
        self._conds_guard = threading.Lock()
        self._signal_counts: dict[str, int] = {}

    def send(self, msg: Message) -> str:
        resolved = self._prepare_message(msg)
        self._append(resolved.to_agent, resolved)
        return resolved.id

    def receive(self, agent_name: str, since_id: str | None = None) -> list[Message]:
        normalized_name = agent_name.strip()
        if not normalized_name:
            raise ValueError("agent_name must not be empty")

        mailbox_path = self._mailbox_path(normalized_name)
        self.ensure_mailbox(normalized_name)
        with self._lock_for(normalized_name):
            lines = mailbox_path.read_text(encoding="utf-8").splitlines()

        messages: list[Message] = []
        found_cursor = since_id is None
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid mailbox entry in {mailbox_path} at line {index}: {exc}"
                ) from exc
            message = Message.from_dict(payload)
            if not found_cursor:
                if message.id == since_id:
                    found_cursor = True
                continue
            messages.append(message)
        return messages

    def ensure_mailbox(self, agent_name: str) -> Path:
        normalized_name = agent_name.strip()
        if not normalized_name:
            raise ValueError("agent_name must not be empty")
        mailbox_path = self._mailbox_path(normalized_name)
        mailbox_path.parent.mkdir(parents=True, exist_ok=True)
        mailbox_path.touch(exist_ok=True)
        return mailbox_path

    def list_agents(self) -> list[str]:
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        return sorted(path.stem for path in self.mailbox_dir.glob("*.jsonl"))

    def latest_message_id(self, agent_name: str) -> str | None:
        normalized_name = agent_name.strip()
        if not normalized_name:
            raise ValueError("agent_name must not be empty")

        mailbox_path = self.ensure_mailbox(normalized_name)
        with self._lock_for(normalized_name):
            lines = mailbox_path.read_text(encoding="utf-8").splitlines()

        for index, line in enumerate(reversed(lines), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid mailbox entry in {mailbox_path} near tail line {index}: {exc}"
                ) from exc
            message = Message.from_dict(payload)
            return message.id
        return None

    def find_message(self, message_id: str) -> Message | None:
        normalized_id = message_id.strip()
        if not normalized_id:
            raise ValueError("message_id must not be empty")

        with self._index_lock:
            cached = self._message_index.get(normalized_id)
        if cached is not None:
            return cached[1]

        for agent_name in self.list_agents():
            for message in self.receive(agent_name):
                with self._index_lock:
                    self._message_index[message.id] = (agent_name, message)
                    if len(self._message_index) > self._max_index_size:
                        self._message_index.popitem(last=False)
                if message.id == normalized_id:
                    return message
        return None

    def _append(self, agent_name: str, msg: Message) -> None:
        mailbox_path = self.ensure_mailbox(agent_name)
        line = json.dumps(msg.to_dict(), ensure_ascii=False)
        with self._lock_for(agent_name):
            with mailbox_path.open("a", encoding="utf-8") as file:
                file.write(line)
                file.write("\n")
        with self._index_lock:
            self._message_index[msg.id] = (agent_name, msg)
            if len(self._message_index) > self._max_index_size:
                self._message_index.popitem(last=False)
        cond = self._cond_for(agent_name)
        with cond:
            self._signal_counts[agent_name] = self._signal_counts.get(agent_name, 0) + 1
            cond.notify_all()

    def _cond_for(self, agent_name: str) -> threading.Condition:
        with self._conds_guard:
            cond = self._conds.get(agent_name)
            if cond is None:
                cond = threading.Condition()
                self._conds[agent_name] = cond
            return cond

    def wait_for_message(self, agent_name: str, timeout: float) -> None:
        """Block until a message is appended to *agent_name*'s mailbox or *timeout* elapses."""
        cond = self._cond_for(agent_name)
        with cond:
            initial = self._signal_counts.get(agent_name, 0)
            cond.wait_for(lambda: self._signal_counts.get(agent_name, 0) != initial, timeout=timeout)

    def _mailbox_path(self, agent_name: str) -> Path:
        return self.mailbox_dir / f"{agent_name}.jsonl"

    def _lock_for(self, agent_name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(agent_name)
            if lock is None:
                lock = threading.Lock()
                self._locks[agent_name] = lock
            return lock

    def _prepare_message(self, msg: Message) -> Message:
        if not msg.from_agent.strip():
            raise ValueError("from_agent must not be empty")
        if not msg.to_agent.strip():
            raise ValueError("to_agent must not be empty")
        if not msg.msg_type.strip():
            raise ValueError("msg_type must not be empty")

        message_id = msg.id.strip() or _generate_message_id()
        timestamp = msg.timestamp.strip() or _timestamp()
        return Message(
            id=message_id,
            from_agent=msg.from_agent.strip(),
            to_agent=msg.to_agent.strip(),
            content=msg.content,
            msg_type=msg.msg_type.strip(),
            timestamp=timestamp,
            in_reply_to=optional_string(msg.in_reply_to),
        )


def _generate_message_id(length: int = 12) -> str:
    return "".join(secrets.choice(_MESSAGE_ID_ALPHABET) for _ in range(length))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
