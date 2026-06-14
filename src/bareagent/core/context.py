from __future__ import annotations

import subprocess
from datetime import date
from functools import lru_cache
from pathlib import Path

BASE_SYSTEM_PROMPT = "You are BareAgent, a terminal-based coding assistant."

# Injected into the system context only while the permission mode is PLAN
# (see ``main.py:_refresh_plan_directive``). Tells the model how the plan-mode
# workflow works: research read-only, then present a plan via ``exit_plan_mode``
# rather than blindly attempting write tools (which PLAN blocks).
PLAN_MODE_DIRECTIVE = (
    "You are in PLAN mode. Investigate and design only -- do NOT modify files, "
    "run state-changing commands, or perform other side effects. Use the "
    "read-only tools (read_file, glob, grep, web_fetch, web_search, load_skill) "
    "to research the task thoroughly.\n"
    "When your implementation plan is ready, call the exit_plan_mode tool with "
    "the full plan as markdown to present it for approval. That tool is the only "
    "way to leave plan mode -- do not ask for approval in plain prose.\n"
    "If the user approves, the permission mode switches and you continue with the "
    "implementation in this same conversation. If the user rejects, you stay in "
    "PLAN mode: revise the plan using their feedback and call exit_plan_mode again."
)


def _normalize_workspace(workspace: Path) -> Path:
    return workspace.expanduser().resolve()


def _run_git_command(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    return completed.stdout.strip()


@lru_cache(maxsize=1)
def _get_system_context_cached(workspace: Path) -> str:
    try:
        branch = _run_git_command(workspace, "branch", "--show-current") or "detached"
    except (OSError, subprocess.SubprocessError):
        branch = "unknown"

    try:
        recent_commits = (
            _run_git_command(workspace, "log", "--oneline", "-3") or "No commits found."
        )
    except (OSError, subprocess.SubprocessError):
        recent_commits = "No commits found." if branch != "unknown" else "Unavailable."

    return "\n".join(
        [
            f"Git branch: {branch}",
            "Recent commits:",
            recent_commits,
        ]
    )


def get_system_context(workspace: Path | None = None) -> str:
    """Return git metadata for the requested workspace without repeating git calls."""
    resolved_workspace = _normalize_workspace(workspace or Path.cwd())
    cached = _get_system_context_cached(resolved_workspace)
    return f"{cached}\nDate: {date.today().isoformat()}"


def _read_context_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_user_context(workspace: Path) -> str:
    """Load global and workspace-level BAREAGENT.md files."""
    workspace = _normalize_workspace(workspace)
    context_files = [
        Path.home() / ".bareagent" / "BAREAGENT.md",
        workspace / "BAREAGENT.md",
    ]

    sections: list[str] = []
    for path in context_files:
        content = _read_context_file(path)
        if content:
            sections.append(f"# From {path}\n{content}")

    return "\n\n".join(sections)


def assemble_system_prompt(
    workspace: Path,
    skill_summary: str = "",
    nag_reminder: str = "",
    memory_context: str = "",
) -> str:
    """Assemble the full system prompt from dynamic context fragments."""
    workspace = _normalize_workspace(workspace)
    sections = [
        BASE_SYSTEM_PROMPT,
        f"Workspace: {workspace}",
        get_system_context(workspace),
    ]

    user_context = get_user_context(workspace)
    if user_context:
        sections.append(f"<user-instructions>\n{user_context}\n</user-instructions>")

    if memory_context.strip():
        sections.append(memory_context.strip())

    if skill_summary.strip():
        sections.append(f"<skill-summary>\n{skill_summary.strip()}\n</skill-summary>")

    if nag_reminder.strip():
        sections.append(f"<nag-reminder>\n{nag_reminder.strip()}\n</nag-reminder>")

    return "\n\n".join(sections)
