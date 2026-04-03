from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
import subprocess

BASE_SYSTEM_PROMPT = "You are BareAgent, a terminal-based coding assistant."


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

    if skill_summary.strip():
        sections.append(f"<skill-summary>\n{skill_summary.strip()}\n</skill-summary>")

    if nag_reminder.strip():
        sections.append(f"<nag-reminder>\n{nag_reminder.strip()}\n</nag-reminder>")

    return "\n\n".join(sections)
