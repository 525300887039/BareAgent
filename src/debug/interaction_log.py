from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_DEFAULT_EVENT_QUEUE_SIZE = 256


class InteractionLogger:
    """Persist complete LLM request/response payloads for a session."""

    def __init__(
        self,
        log_dir: str | Path = ".logs",
        session_id: str = "default",
        *,
        pretty: bool = True,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._session_id = self._validate_session_id(session_id)
        self._pretty = pretty
        self._seq = 0
        self._session_dir: Path | None = None
        self._event_lock = threading.Lock()
        self._event_subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._legacy_event_queue: queue.Queue[dict[str, Any]] | None = None

    @property
    def event_queue(self) -> queue.Queue[dict[str, Any]]:
        if self._legacy_event_queue is None:
            self._legacy_event_queue = self.subscribe_events()
        return self._legacy_event_queue

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session_id = self._validate_session_id(value)
        self._session_dir = None
        self._seq = 0

    def log_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        provider_info: dict[str, Any] | None = None,
    ) -> int:
        self._ensure_session_dir()
        seq = self._seq
        payload = {
            "seq": seq,
            "type": "request",
            "timestamp": time.time(),
            "provider": provider_info or {},
            "messages": messages,
            "tools": tools,
            "message_count": len(messages),
            "tool_count": len(tools),
        }
        self._write(f"{seq:03d}_request.json", payload)
        self._seq = seq + 1
        self._push_event("request", seq, payload)
        return seq

    def log_response(
        self,
        seq: int,
        *,
        text: str = "",
        thinking: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: float = 0,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "seq": seq,
            "type": "response",
            "timestamp": time.time(),
            "text": text,
            "thinking": thinking,
            "tool_calls": list(tool_calls or []),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": round(duration_ms, 2),
        }
        if error is not None:
            payload["error"] = error
        try:
            self._write(f"{seq:03d}_response.json", payload)
        finally:
            self._seq = max(self._seq, seq + 1)
        self._push_event("response", seq, payload)

    def list_sessions(self) -> list[str]:
        if not self._log_dir.is_dir():
            return []
        return sorted(path.name for path in self._log_dir.iterdir() if path.is_dir())

    def list_interactions(self, session_id: str) -> list[dict[str, Any]]:
        session_dir = self._session_path(session_id)
        if not session_dir.is_dir():
            return []

        interactions: list[dict[str, Any]] = []
        for request_path in sorted(
            session_dir.glob("*_request.json"),
            key=self._path_seq,
        ):
            seq = self._path_seq(request_path)
            if seq < 0:
                continue

            request_data = self._read_json(request_path) or {}
            entry: dict[str, Any] = {
                "seq": seq,
                "timestamp": request_data.get("timestamp"),
                "message_count": request_data.get("message_count", 0),
                "tool_count": request_data.get("tool_count", 0),
            }

            response_data = self._read_json(session_dir / f"{seq:03d}_response.json")
            if response_data is not None:
                entry.update(
                    {
                        "input_tokens": response_data.get("input_tokens", 0),
                        "output_tokens": response_data.get("output_tokens", 0),
                        "duration_ms": response_data.get("duration_ms", 0),
                        "tool_call_count": len(response_data.get("tool_calls", [])),
                        "has_error": "error" in response_data,
                    }
                )

            interactions.append(entry)

        return interactions

    def get_interaction(self, session_id: str, seq: int) -> dict[str, Any]:
        session_dir = self._session_path(session_id)
        return {
            "seq": seq,
            "request": self._read_json(session_dir / f"{seq:03d}_request.json"),
            "response": self._read_json(session_dir / f"{seq:03d}_response.json"),
        }

    def subscribe_events(
        self,
        *,
        maxsize: int = _DEFAULT_EVENT_QUEUE_SIZE,
    ) -> queue.Queue[dict[str, Any]]:
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._event_lock:
            self._event_subscribers.add(event_queue)
        return event_queue

    def unsubscribe_events(self, event_queue: queue.Queue[dict[str, Any]]) -> None:
        with self._event_lock:
            self._event_subscribers.discard(event_queue)
            if event_queue is self._legacy_event_queue:
                self._legacy_event_queue = None

    def _ensure_session_dir(self) -> Path:
        if self._session_dir is None:
            self._session_dir = self._session_path(self._session_id)
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._seq = self._discover_next_seq(self._session_dir)
        return self._session_dir

    def _session_path(self, session_id: str) -> Path:
        return self._log_dir / self._validate_session_id(session_id)

    def _validate_session_id(self, session_id: str) -> str:
        value = str(session_id)
        if value in {"", ".", ".."}:
            raise ValueError("Session ID must be a single relative path segment.")
        if "/" in value or "\\" in value:
            raise ValueError("Session ID must not contain path separators.")

        posix_path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if posix_path.is_absolute() or windows_path.is_absolute():
            raise ValueError("Session ID must not be an absolute path.")
        if posix_path.anchor or windows_path.anchor:
            raise ValueError("Session ID must not include a path anchor.")
        if len(posix_path.parts) != 1 or len(windows_path.parts) != 1:
            raise ValueError("Session ID must be a single path segment.")
        return value

    def _discover_next_seq(self, session_dir: Path) -> int:
        max_seq = -1
        for path in session_dir.glob("*_*.json"):
            max_seq = max(max_seq, self._path_seq(path))
        return max_seq + 1

    def _path_seq(self, path: Path) -> int:
        seq_text = path.stem.split("_", 1)[0]
        return int(seq_text) if seq_text.isdigit() else -1

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            return data
        return None

    def _write(self, filename: str, payload: dict[str, Any]) -> None:
        session_dir = self._ensure_session_dir()
        path = session_dir / filename
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2 if self._pretty else None,
                default=str,
            ),
            encoding="utf-8",
        )

    def _push_event(self, event_type: str, seq: int, payload: dict[str, Any]) -> None:
        event = {
            "event": event_type,
            "session_id": self._session_id,
            "seq": seq,
            "timestamp": payload.get("timestamp"),
        }
        with self._event_lock:
            subscribers = tuple(self._event_subscribers)

        for event_queue in subscribers:
            self._publish_event(event_queue, event)

    def _publish_event(
        self,
        event_queue: queue.Queue[dict[str, Any]],
        event: dict[str, Any],
    ) -> None:
        try:
            event_queue.put_nowait(event)
            return
        except queue.Full:
            pass

        try:
            event_queue.get_nowait()
        except queue.Empty:
            return

        try:
            event_queue.put_nowait(event)
        except queue.Full:
            return
