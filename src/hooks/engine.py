"""Hook execution engine.

Matches configured hooks against a tool call and runs them as cross-platform
subprocesses, passing the call context as a single JSON object on stdin. The
control protocol is exit-code based (PRD D2):

- ``PreToolUse`` exit 2 -> intercept: the handler is skipped and the hook's
  stderr is returned to the LLM as an error result.
- exit 0 -> allow.
- any other non-zero exit -> non-blocking warning, then allow.

Failures are fail-open (PRD D3): a spawn error or timeout warns and allows; it
never blocks the tool or crashes the loop. ``PostToolUse`` hooks run side
effects only — their exit code never changes the tool result.

This module must NOT import :mod:`src.core.loop` — the engine is *called by* the
loop, and the reverse dependency would create an import cycle.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from src.core.fileutil import stringify
from src.ui.protocol import UIProtocol

from .config import HookEntry, HooksConfig
from .events import HookEvent

# Exit code reserved by the Claude-Code-aligned protocol for "intercept".
_BLOCK_EXIT_CODE = 2


@dataclass(slots=True)
class HookOutcome:
    """Result of running the PreToolUse hooks for one tool call."""

    block: bool = False
    reason: str = ""


class HookEngine:
    """Runs PreToolUse / PostToolUse hooks for the main agent loop."""

    def __init__(self, config: HooksConfig, *, console: UIProtocol | None = None) -> None:
        self._config = config
        self._console = console

    def run_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        session_id: str,
        cwd: str,
    ) -> HookOutcome:
        """Run matching PreToolUse hooks. First exit-2 hook intercepts the call."""
        entries = self._config.matching(HookEvent.PRE_TOOL_USE.value, tool_name)
        if not entries:
            return HookOutcome()

        payload = {
            "event": HookEvent.PRE_TOOL_USE.value,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": session_id,
            "cwd": cwd,
        }
        for entry in entries:
            result = self._run_one(entry, payload)
            if result is None:
                # Spawn failure / timeout -> fail-open, already warned.
                continue
            if result.returncode == _BLOCK_EXIT_CODE:
                reason = (result.stderr or "").strip()
                return HookOutcome(
                    block=True,
                    reason=reason or f"Blocked by PreToolUse hook for {tool_name}.",
                )
            if result.returncode != 0:
                self._warn_non_zero(entry, tool_name, result)
        return HookOutcome()

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any,
        *,
        is_error: bool,
        session_id: str,
        cwd: str,
    ) -> None:
        """Run matching PostToolUse hooks for side effects. Exit codes don't matter."""
        entries = self._config.matching(HookEvent.POST_TOOL_USE.value, tool_name)
        if not entries:
            return

        payload = {
            "event": HookEvent.POST_TOOL_USE.value,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": session_id,
            "cwd": cwd,
            "tool_output": stringify(tool_output),
            "is_error": is_error,
        }
        for entry in entries:
            result = self._run_one(entry, payload)
            if result is None:
                continue
            if result.returncode != 0:
                self._warn_non_zero(entry, tool_name, result)

    def _run_one(
        self, entry: HookEntry, payload: dict[str, Any]
    ) -> subprocess.CompletedProcess[str] | None:
        """Spawn the hook command, feeding *payload* as JSON on stdin.

        Returns the completed process, or ``None`` when the hook failed to run
        at all (timeout / spawn error) — both fail-open cases (PRD D3).
        """
        argv = _build_argv(entry.command)
        stdin_json = json.dumps(payload, ensure_ascii=False)
        try:
            return subprocess.run(
                argv,
                input=stdin_json,
                capture_output=True,
                timeout=entry.timeout,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            self._warn(
                f"{entry.event} hook timed out after {entry.timeout}s "
                f"(command: {entry.command!r}); allowing tool."
            )
            return None
        except (OSError, ValueError) as exc:
            self._warn(
                f"{entry.event} hook failed to start ({type(exc).__name__}: {exc}); allowing tool."
            )
            return None

    def _warn_non_zero(
        self,
        entry: HookEntry,
        tool_name: str,
        result: subprocess.CompletedProcess[str],
    ) -> None:
        detail = (result.stderr or result.stdout or "").strip()
        message = (
            f"{entry.event} hook for {tool_name} exited with code "
            f"{result.returncode} (non-blocking)."
        )
        if detail:
            message = f"{message} {detail}"
        self._warn(message)

    def _warn(self, message: str) -> None:
        if self._console is not None:
            self._console.print_status(message)


def _build_argv(command: str) -> list[str]:
    """Build the cross-platform shell argv for *command*.

    Mirrors :func:`src.core.handlers.bash.run_bash`: Windows PowerShell with the
    output encoding forced to UTF-8 so non-ASCII stdout/stderr round-trips, and
    ``bash -lc`` elsewhere.

    Unlike ``run_bash`` (which only reads ``returncode`` for a status message),
    the hook control protocol *depends* on the child's exit code, so the
    PowerShell wrapper appends ``; exit $LASTEXITCODE``. Without it,
    ``powershell -Command`` returns its own parse/host exit status (typically 0
    or 1) rather than the command's exit code — which would silently break the
    exit-2 intercept contract.
    """
    if os.name == "nt":
        windows_prefix = (
            "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}; "
        )
        windows_suffix = "; exit $LASTEXITCODE"
        return [
            "powershell",
            "-NoProfile",
            "-Command",
            windows_prefix + command + windows_suffix,
        ]
    return ["bash", "-lc", command]
