from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from src.planning.skills import SkillLoader, resolve_skills_dir
from src.planning.subagent import run_subagent
from src.planning.todo import TodoManager
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


def test_todo_manager_add_update_and_list_flow() -> None:
    manager = TodoManager()

    added_one = manager.add("Create planner")
    added_two = manager.add("Write tests", priority="high")

    assert added_one == "Added TODO t1 [normal]: Create planner"
    assert added_two == "Added TODO t2 [high]: Write tests"
    assert manager.tasks == {
        "t1": {
            "task": "Create planner",
            "status": "pending",
            "priority": "normal",
        },
        "t2": {
            "task": "Write tests",
            "status": "pending",
            "priority": "high",
        },
    }

    assert manager.update("t1", "in_progress") == "Updated TODO t1 -> in_progress"
    assert manager.update("t1", "done") == "Updated TODO t1 -> done"

    listing = manager.list()
    assert listing == "\n".join(
        [
            "TODO items:",
            "- t1 [done] (normal) Create planner",
            "- t2 [pending] (high) Write tests",
        ]
    )


def test_todo_manager_nag_reminder_for_open_and_completed_items() -> None:
    manager = TodoManager()

    assert manager.get_nag_reminder() is None

    manager.add("Implement TODO tool")
    manager.add("Document planner", priority="low")

    reminder = manager.get_nag_reminder()

    assert reminder is not None
    assert "unfinished TODO items" in reminder
    assert "- t1 [pending] (normal) Implement TODO tool" in reminder
    assert "- t2 [pending] (low) Document planner" in reminder

    manager.update("t1", "done")
    manager.update("t2", "done")

    assert manager.get_nag_reminder() is None


def test_skill_loader_scan_load_and_prompt(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    git_dir = skills_dir / "git"
    test_dir = skills_dir / "test"
    git_dir.mkdir(parents=True)
    test_dir.mkdir(parents=True)
    (git_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "# Git",
                "",
                "Git workflow conventions for branches and commits.",
                "",
                "- Use feat/fix scopes.",
            ]
        ),
        encoding="utf-8",
    )
    (test_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "# Testing",
                "",
                "Testing guidance for unit and integration coverage.",
                "",
                "- Prefer AAA.",
            ]
        ),
        encoding="utf-8",
    )

    loader = SkillLoader(skills_dir)

    skills = loader.scan()

    assert [(skill.skill_name, skill.description) for skill in skills] == [
        ("git", "Git workflow conventions for branches and commits."),
        ("test", "Testing guidance for unit and integration coverage."),
    ]
    assert "Available skills" in loader.get_skill_list_prompt()
    assert "- git: Git workflow conventions for branches and commits." in loader.get_skill_list_prompt()
    assert loader.load("git").startswith("# Git")

    with pytest.raises(ValueError, match="Unknown skill: missing"):
        loader.load("missing")


def test_run_subagent_keeps_parent_system_prompt() -> None:
    provider = RecordingProvider()

    result = run_subagent(
        provider=provider,
        task="Inspect the repo",
        tools=[],
        handlers={},
        permission=None,
        system_prompt="You are BareAgent with repo instructions.",
    )

    assert result == "subagent done"
    assert provider.messages == [
        {"role": "system", "content": "You are BareAgent with repo instructions."},
        {"role": "user", "content": "Inspect the repo"},
    ]


def test_resolve_skills_dir_points_to_bundled_skills() -> None:
    skills_dir = resolve_skills_dir()

    assert skills_dir.name == "skills"
    assert (skills_dir / "git" / "SKILL.md").exists()


def test_pyproject_packages_skills_for_distributions() -> None:
    pyproject = Path("pyproject.toml")
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    wheel_force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    sdist_include = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert wheel_force_include["skills"] == "skills"
    assert "skills" in sdist_include
