"""Tests for src.hooks.engine — running hook subprocesses and the exit-code protocol.

Hook commands invoke a small python *script file* with the current interpreter
(``python "<path>"``). A single quoted path survives both ``bash -lc`` and
``powershell -Command`` cleanly, whereas inline ``python -c "..."`` source does
not (PowerShell re-parses the inner quotes / semicolons). This keeps the
subprocess behavior stable across Windows / Linux / macOS without depending on
shell-specific quoting.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest

from src.hooks.config import HookEntry, HooksConfig
from src.hooks.engine import HookEngine
from src.ui.protocol import UIProtocol


class FakeConsole:
    """Minimal console capturing only the warnings the engine emits."""

    def __init__(self) -> None:
        self.statuses: list[str] = []

    def print_status(self, msg: str) -> None:
        self.statuses.append(msg)


def _script(tmp_path: Path, name: str, source: str) -> str:
    """Write *source* to a .py file and return a command running it.

    Targets the *exact* running interpreter (``sys.executable``) so the test is
    deterministic regardless of what a bare ``python`` resolves to on PATH
    (under the test runner that may be a stub that doesn't forward stdin). On
    Windows a leading quoted token is a PowerShell parse error, so the ``&``
    call operator is prepended there; ``bash -lc`` accepts the quoted path
    directly. The engine itself handles exit-code propagation in its own
    ``_build_argv`` wrapper, so the test command stays just "run this script".
    """
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        return f'& "{sys.executable}" "{script}"'
    return f'"{sys.executable}" "{script}"'


def _engine(entries: list[HookEntry], console: FakeConsole | None = None) -> HookEngine:
    ui = cast(UIProtocol, console) if console is not None else None
    return HookEngine(HooksConfig(entries=entries), console=ui)


# --- PreToolUse exit-code protocol ---------------------------------------


def test_pre_tool_use_exit_2_blocks_with_stderr_reason(tmp_path: Path) -> None:
    cmd = _script(
        tmp_path,
        "block.py",
        "import sys\nsys.stderr.write('dangerous rm -rf')\nsys.exit(2)\n",
    )
    engine = _engine([HookEntry(event="PreToolUse", command=cmd, tool="bash")])

    outcome = engine.run_pre_tool_use("bash", {"command": "rm -rf /"}, session_id="s1", cwd=".")

    assert outcome.block is True
    assert "dangerous rm -rf" in outcome.reason


def test_pre_tool_use_exit_0_allows(tmp_path: Path) -> None:
    cmd = _script(tmp_path, "ok.py", "import sys\nsys.exit(0)\n")
    engine = _engine([HookEntry(event="PreToolUse", command=cmd)])
    outcome = engine.run_pre_tool_use("bash", {}, session_id="s1", cwd=".")
    assert outcome.block is False
    assert outcome.reason == ""


def test_pre_tool_use_other_nonzero_warns_but_allows(tmp_path: Path) -> None:
    console = FakeConsole()
    cmd = _script(tmp_path, "boom.py", "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
    engine = _engine([HookEntry(event="PreToolUse", command=cmd)], console=console)

    outcome = engine.run_pre_tool_use("bash", {}, session_id="s1", cwd=".")

    assert outcome.block is False
    assert any("exited with code 1" in s for s in console.statuses)


def test_pre_tool_use_no_match_is_noop(tmp_path: Path) -> None:
    cmd = _script(tmp_path, "block.py", "import sys\nsys.exit(2)\n")
    engine = _engine([HookEntry(event="PreToolUse", command=cmd, tool="write_file")])
    # tool name differs -> no hook runs -> not blocked.
    outcome = engine.run_pre_tool_use("bash", {}, session_id="s1", cwd=".")
    assert outcome.block is False


def test_pre_tool_use_first_block_wins(tmp_path: Path) -> None:
    engine = _engine(
        [
            HookEntry(
                event="PreToolUse",
                command=_script(tmp_path, "first.py", "import sys\nsys.exit(0)\n"),
            ),
            HookEntry(
                event="PreToolUse",
                command=_script(
                    tmp_path,
                    "second.py",
                    "import sys\nsys.stderr.write('second')\nsys.exit(2)\n",
                ),
            ),
        ]
    )
    outcome = engine.run_pre_tool_use("bash", {}, session_id="s1", cwd=".")
    assert outcome.block is True
    assert "second" in outcome.reason


# --- JSON stdin payload --------------------------------------------------


def _dump_payload_script(tmp_path: Path, name: str, out_file: Path) -> str:
    # Read stdin as raw UTF-8 bytes — the child interpreter's text stdin uses
    # the OS locale codec (GBK on a zh-CN Windows console), which would
    # mis-decode the non-ASCII JSON the engine writes. A real hook should do
    # the same (decode UTF-8 explicitly / json.load(sys.stdin.buffer)).
    source = (
        "import sys, pathlib\n"
        "data = sys.stdin.buffer.read().decode('utf-8')\n"
        f"pathlib.Path({json.dumps(str(out_file))}).write_text(data, encoding='utf-8')\n"
    )
    return _script(tmp_path, name, source)


def test_pre_tool_use_receives_json_stdin(tmp_path: Path) -> None:
    out_file = tmp_path / "payload.json"
    cmd = _dump_payload_script(tmp_path, "dump.py", out_file)
    engine = _engine([HookEntry(event="PreToolUse", command=cmd, tool="bash")])

    engine.run_pre_tool_use(
        "bash",
        {"command": "ls", "extra": "値"},
        session_id="sess-123",
        cwd="/work",
    )

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["event"] == "PreToolUse"
    assert payload["tool_name"] == "bash"
    assert payload["tool_input"] == {"command": "ls", "extra": "値"}
    assert payload["session_id"] == "sess-123"
    assert payload["cwd"] == "/work"
    assert "tool_output" not in payload


def test_post_tool_use_payload_includes_output_and_is_error(tmp_path: Path) -> None:
    out_file = tmp_path / "payload.json"
    cmd = _dump_payload_script(tmp_path, "dump.py", out_file)
    engine = _engine([HookEntry(event="PostToolUse", command=cmd)])

    engine.run_post_tool_use(
        "write_file",
        {"file_path": "a.py"},
        "wrote 3 lines",
        is_error=False,
        session_id="sess-9",
        cwd=".",
    )

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["event"] == "PostToolUse"
    assert payload["tool_name"] == "write_file"
    assert payload["tool_output"] == "wrote 3 lines"
    assert payload["is_error"] is False


# --- PostToolUse side effects --------------------------------------------


def test_post_tool_use_runs_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    source = (
        "import sys, pathlib\n"
        f"pathlib.Path({json.dumps(str(marker))}).write_text('done', encoding='utf-8')\n"
    )
    cmd = _script(tmp_path, "side.py", source)
    engine = _engine([HookEntry(event="PostToolUse", command=cmd, tool="write_file")])

    engine.run_post_tool_use("write_file", {}, "ok", is_error=False, session_id="s", cwd=".")

    assert marker.read_text(encoding="utf-8") == "done"


def test_post_tool_use_nonzero_warns_but_returns_none(tmp_path: Path) -> None:
    console = FakeConsole()
    cmd = _script(tmp_path, "exit3.py", "import sys\nsys.exit(3)\n")
    engine = _engine([HookEntry(event="PostToolUse", command=cmd)], console=console)

    result = engine.run_post_tool_use("bash", {}, "out", is_error=False, session_id="s", cwd=".")

    assert result is None
    assert any("exited with code 3" in s for s in console.statuses)


# --- fail-open: timeout / spawn failure ----------------------------------


def test_pre_tool_use_timeout_fails_open(tmp_path: Path) -> None:
    console = FakeConsole()
    cmd = _script(tmp_path, "slow.py", "import time\ntime.sleep(5)\n")
    engine = _engine(
        [HookEntry(event="PreToolUse", command=cmd, timeout=1)],
        console=console,
    )

    outcome = engine.run_pre_tool_use("bash", {}, session_id="s", cwd=".")

    assert outcome.block is False  # fail-open: timed out hook does not block
    assert any("timed out" in s for s in console.statuses)


def test_pre_tool_use_spawn_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    console = FakeConsole()
    engine = _engine([HookEntry(event="PreToolUse", command="whatever")], console=console)

    import subprocess as _subprocess

    def _boom(*args, **kwargs):
        raise FileNotFoundError("no such shell")

    monkeypatch.setattr(_subprocess, "run", _boom)

    outcome = engine.run_pre_tool_use("bash", {}, session_id="s", cwd=".")

    assert outcome.block is False
    assert any("failed to start" in s for s in console.statuses)
