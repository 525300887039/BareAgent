from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_bash(command: str, timeout: int = 30, *, cwd: Path | None = None) -> str:
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
        )
    except subprocess.TimeoutExpired as exc:
        output = _join_output(exc.stdout, exc.stderr)
        if output:
            return f"Error: command timed out after {timeout} seconds\n{output}"
        return f"Error: command timed out after {timeout} seconds"

    output = _join_output(result.stdout, result.stderr)
    if result.returncode != 0:
        if output:
            return f"Command failed with exit code {result.returncode}\n{output}"
        return f"Command failed with exit code {result.returncode}"
    return output


def _join_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts: list[str] = []
    for value in (stdout, stderr):
        if value is None:
            continue
        if isinstance(value, bytes):
            decoded = value.decode("utf-8", errors="replace")
        else:
            decoded = value
        text = decoded.rstrip()
        if text:
            parts.append(text)
    return "\n".join(parts)
