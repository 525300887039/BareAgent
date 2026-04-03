from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.core.fileutil import atomic_write_json


@dataclass(slots=True)
class Teammate:
    name: str
    role: str
    system_prompt: str
    provider_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentInstance:
    name: str
    role: str
    system_prompt: str
    provider: Any
    provider_config: dict[str, Any] = field(default_factory=dict)


class TeammateManager:
    """Persist teammate definitions and spawn independent agent instances."""

    def __init__(self, config_file: str | Path = ".team.json") -> None:
        self.config_file = Path(config_file)
        self.teammates: dict[str, Teammate] = {}
        self._lock = threading.RLock()
        self._load()

    @classmethod
    def create_empty(cls, config_file: str | Path) -> "TeammateManager":
        instance = cls.__new__(cls)
        instance.config_file = Path(config_file)
        instance.teammates = {}
        instance._lock = threading.RLock()
        return instance

    def register(
        self,
        name: str,
        role: str,
        system_prompt: str,
        provider_config: dict[str, Any] | None = None,
    ) -> Teammate:
        with self._lock:
            normalized_name = name.strip()
            normalized_role = role.strip()
            normalized_prompt = system_prompt.strip()
            if not normalized_name:
                raise ValueError("name must not be empty")
            if not normalized_role:
                raise ValueError("role must not be empty")
            if not normalized_prompt:
                raise ValueError("system_prompt must not be empty")

            teammate = Teammate(
                name=normalized_name,
                role=normalized_role,
                system_prompt=normalized_prompt,
                provider_config=dict(provider_config or {}),
            )
            self.teammates[normalized_name] = teammate
            self._save()
            return teammate

    def get(self, name: str) -> Teammate:
        with self._lock:
            teammate = self.teammates.get(name.strip())
            if teammate is None:
                raise ValueError(f"Unknown teammate: {name}")
            return teammate

    def list(self) -> list[Teammate]:
        with self._lock:
            return sorted(self.teammates.values(), key=lambda teammate: teammate.name)

    def spawn(
        self,
        name: str,
        provider_factory: Callable[[dict[str, Any]], Any],
    ) -> AgentInstance:
        with self._lock:
            teammate = self.get(name)
            provider_config = dict(teammate.provider_config)
            snapshot_name = teammate.name
            snapshot_role = teammate.role
            snapshot_prompt = teammate.system_prompt
        provider = provider_factory(provider_config)
        return AgentInstance(
            name=snapshot_name,
            role=snapshot_role,
            system_prompt=snapshot_prompt,
            provider=provider,
            provider_config=provider_config,
        )

    def _save(self) -> None:
        payload = {
            "teammates": {
                teammate.name: teammate.to_dict()
                for teammate in self.list()
            }
        }
        atomic_write_json(self.config_file, payload)

    def _load(self) -> None:
        if not self.config_file.exists():
            self.teammates = {}
            return

        with self.config_file.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if not isinstance(payload, dict):
            raise ValueError("Team config must contain a JSON object")

        raw_teammates = payload.get("teammates", {})
        if not isinstance(raw_teammates, dict):
            raise ValueError("Team config 'teammates' field must be an object")

        loaded: dict[str, Teammate] = {}
        for teammate_name, raw_teammate in raw_teammates.items():
            if not isinstance(raw_teammate, dict):
                raise ValueError(f"Invalid teammate payload for {teammate_name}")
            provider_config = raw_teammate.get("provider_config") or {}
            if not isinstance(provider_config, dict):
                raise ValueError(
                    f"Teammate provider_config must be an object: {teammate_name}"
                )
            teammate = Teammate(
                name=str(raw_teammate.get("name", teammate_name)).strip(),
                role=str(raw_teammate.get("role", "")).strip(),
                system_prompt=str(raw_teammate.get("system_prompt", "")).strip(),
                provider_config=dict(provider_config),
            )
            if not teammate.name:
                raise ValueError(f"Teammate name must not be empty: {teammate_name}")
            if not teammate.role:
                raise ValueError(f"Teammate role must not be empty: {teammate_name}")
            if not teammate.system_prompt:
                raise ValueError(
                    f"Teammate system_prompt must not be empty: {teammate_name}"
                )
            loaded[teammate.name] = teammate

        self.teammates = loaded
