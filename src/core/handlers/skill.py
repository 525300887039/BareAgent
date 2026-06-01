"""Handler + schema for the ``skill_create`` tool (experiential skill drafting).

Unlike most tools, ``skill_create`` is NOT registered in the global tool set.
It is exposed only inside the isolated "reflection" ``agent_loop`` call that
runs after a sufficiently complex multi-turn task (see ``main.py`` and
``src/planning/skill_gen.py``). Keeping it out of the global set means:
- the main loop never offers it, so skills are *triggered*, not spontaneous;
- sub-agents never receive it (isolation, like ``hook_engine``);
- ``[skills] auto_generate = false`` fully short-circuits — the tool simply
  does not exist when the reflection never runs.

The handler is a thin wrapper over :class:`src.planning.skill_store.SkillStore`,
converting expected storage errors into ``Error:`` strings so the model can
react instead of crashing the loop (see ``error-handling.md``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.schema import tool_schema

if TYPE_CHECKING:
    from src.planning.skill_store import SkillStore

SKILL_CREATE_TOOL_SCHEMA = tool_schema(
    "skill_create",
    (
        "Save a reusable skill distilled from the workflow you just completed. "
        "Writes a draft SKILL.md to the pending area; the user promotes it with "
        "/skill keep. Call at most once per reflection."
    ),
    {
        "name": {
            "type": "string",
            "description": "Short kebab-case skill identifier, e.g. 'add-config-section'.",
        },
        "description": {
            "type": "string",
            "description": "One line starting with 'Use this when ...'.",
        },
        "body": {
            "type": "string",
            "description": (
                "Markdown body: Steps / Pitfalls / Verification sections capturing "
                "the procedure, dead-ends hit, and how success was checked."
            ),
        },
    },
    ["name", "description", "body"],
)

_HANDLED_ERRORS = (ValueError, OSError)


def run_skill_create(
    *,
    store: SkillStore,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
) -> str:
    if not name or not str(name).strip():
        return "Error: skill_create requires a non-empty 'name'."
    try:
        return store.create_draft(
            str(name),
            str(description or ""),
            str(body or ""),
        )
    except _HANDLED_ERRORS as exc:
        return f"Error: {exc}"
