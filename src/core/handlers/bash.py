from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_bash(
    command: str,
    timeout: int = 30,
    *,
    cwd: Path | None = None,
    raise_on_error: bool = False,
) -> str:
    """Run a shell command and return combined stdout/stderr."""
    completed_command = (
        ["powershell", "-NoProfile", "-Command", command]
        if os.name == "nt"
        else ["bash", "-lc", command]
    )

    try:
        result = subprocess.run(
            completed_command,
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=None if cwd is None else str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        output = _join_output(exc.stdout, exc.stderr)
        if output:
            message = f"Error: command timed out after {timeout} seconds\n{output}"
        else:
            message = f"Error: command timed out after {timeout} seconds"
        if raise_on_error:
            raise RuntimeError(message) from exc
        return message

    output = _join_output(result.stdout, result.stderr)
    if result.returncode != 0:
        if output:
            message = f"Command failed with exit code {result.returncode}\n{output}"
        else:
            message = f"Command failed with exit code {result.returncode}"
        if raise_on_error:
            raise RuntimeError(message)
        return message
    return output


def _join_output(stdout: str | None, stderr: str | None) -> str:
    parts: list[str] = []
    for value in (stdout, stderr):
        if value is None:
            continue
        text = value.rstrip()
        if text:
            parts.append(text)
    return "\n".join(parts)
