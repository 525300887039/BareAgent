from pathlib import Path
import subprocess

from src.core import context


def test_get_system_context_uses_requested_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_run_git_command(workspace: Path, *args: str) -> str:
        calls.append((workspace, args))
        if args[0] == "branch":
            return "feature/test"
        return "abc123 init"

    context._get_system_context_cached.cache_clear()
    monkeypatch.setattr(context, "_run_git_command", fake_run_git_command)

    system_context = context.get_system_context(tmp_path)

    assert "Git branch: feature/test" in system_context
    assert calls[0][0] == tmp_path.resolve()
    assert calls[1][0] == tmp_path.resolve()


def test_get_system_context_keeps_branch_when_log_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_run_git_command(workspace: Path, *args: str) -> str:
        if args[0] == "branch":
            return "main"
        raise subprocess.CalledProcessError(128, ["git", *args])

    context._get_system_context_cached.cache_clear()
    monkeypatch.setattr(context, "_run_git_command", fake_run_git_command)

    system_context = context.get_system_context(tmp_path)

    assert "Git branch: main" in system_context
    assert "No commits found." in system_context
