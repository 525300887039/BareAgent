from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


LOAD_SKILL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "load_skill",
        "description": "Load the full content of a named SKILL.md file on demand.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "The skill directory name to load.",
                }
            },
            "required": ["skill_name"],
        },
    }
]


@dataclass(slots=True)
class SkillMeta:
    skill_name: str
    description: str
    path: Path


def resolve_skills_dir() -> Path:
    env_path = os.getenv("BAREAGENT_SKILLS_DIR")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())

    module_path = Path(__file__).resolve()
    candidates.extend(
        [
            module_path.parents[2] / "skills",
            module_path.parents[1] / "skills",
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return candidates[0].resolve()


class SkillLoader:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._cache: dict[str, SkillMeta] = {}

    def scan(self) -> list[SkillMeta]:
        skills: list[SkillMeta] = []
        cache: dict[str, SkillMeta] = {}

        if not self.skills_dir.exists():
            self._cache = {}
            return []

        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            skill_name = skill_file.parent.name
            description = self._extract_description(skill_file)
            meta = SkillMeta(
                skill_name=skill_name,
                description=description,
                path=skill_file,
            )
            skills.append(meta)
            cache[skill_name] = meta

        self._cache = cache
        return skills

    def load(self, skill_name: str) -> str:
        meta = self._lookup(skill_name)
        return meta.path.read_text(encoding="utf-8").strip()

    def get_skill_list_prompt(self) -> str:
        skills = self.scan()
        if not skills:
            return "No skills are available."

        lines = [
            "Available skills (load the full SKILL.md only when you need the details):"
        ]
        for skill in skills:
            lines.append(f"- {skill.skill_name}: {skill.description}")
        return "\n".join(lines)

    def _lookup(self, skill_name: str) -> SkillMeta:
        normalized = skill_name.strip()
        if not normalized:
            raise ValueError("skill_name must not be empty")

        meta = self._cache.get(normalized)
        if meta is None:
            self.scan()
            meta = self._cache.get(normalized)
        if meta is None:
            raise ValueError(f"Unknown skill: {skill_name}")
        return meta

    def _extract_description(self, skill_file: Path) -> str:
        for raw_line in skill_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            return line
        return "No description provided."


def make_skill_handlers(skill_loader: SkillLoader) -> dict[str, Any]:
    return {"load_skill": skill_loader.load}
