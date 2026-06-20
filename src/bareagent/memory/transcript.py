from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_SAVE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H-%M-%S-%f"
_TIMESTAMP_FORMATS = (
    _SAVE_TIMESTAMP_FORMAT,
    "%Y-%m-%dT%H-%M-%S",
)


@dataclass(slots=True)
class _TranscriptEntry:
    session_id: str
    timestamp: datetime
    path: Path


class TranscriptManager:
    def __init__(self, transcript_dir: str | Path = ".transcripts") -> None:
        self.transcript_dir = Path(transcript_dir)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)

    def save(self, messages: list[dict[str, Any]], session_id: str) -> Path:
        timestamp = datetime.now().strftime(_SAVE_TIMESTAMP_FORMAT)
        path = self.transcript_dir / f"{session_id}_{timestamp}.jsonl"
        with path.open("w", encoding="utf-8") as file:
            for message in messages:
                file.write(json.dumps(message, ensure_ascii=False))
                file.write("\n")
        return path

    def load(self, session_id: str) -> list[dict[str, Any]]:
        entry = self._get_session_entry(session_id)
        with entry.path.open("r", encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    def list_sessions(self) -> list[str]:
        latest_by_session: dict[str, datetime] = {}
        for entry in self._iter_entries():
            latest_by_session[entry.session_id] = max(
                entry.timestamp,
                latest_by_session.get(entry.session_id, datetime.min),
            )
        return [
            session_id
            for session_id, _ in sorted(
                latest_by_session.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    def get_latest_session(self) -> str | None:
        entries = self._iter_entries()
        if not entries:
            return None
        return max(entries, key=lambda entry: entry.timestamp).session_id

    def resume(self, session_id: str | None = None) -> list[dict[str, Any]]:
        target_session = session_id or self.get_latest_session()
        if target_session is None:
            raise FileNotFoundError("No saved transcripts found.")
        return self.load(target_session)

    def _get_session_entry(self, session_id: str) -> _TranscriptEntry:
        entries = [entry for entry in self._iter_entries() if entry.session_id == session_id]
        if not entries:
            raise FileNotFoundError(f"No transcript found for session: {session_id}")
        return max(entries, key=lambda entry: entry.timestamp)

    def _iter_entries(self) -> list[_TranscriptEntry]:
        entries: list[_TranscriptEntry] = []
        for path in self.transcript_dir.glob("*.jsonl"):
            entry = self._parse_entry(path)
            if entry is not None:
                entries.append(entry)
        return entries

    def _parse_entry(self, path: Path) -> _TranscriptEntry | None:
        stem = path.stem
        if "_" not in stem:
            return None
        session_id, raw_timestamp = stem.rsplit("_", 1)
        timestamp: datetime | None = None
        for fmt in _TIMESTAMP_FORMATS:
            try:
                timestamp = datetime.strptime(raw_timestamp, fmt)
                break
            except ValueError:
                continue
        if timestamp is None:
            return None
        return _TranscriptEntry(session_id=session_id, timestamp=timestamp, path=path)
