"""Tests for the Hybrid auto-diagnostics hook plumbed into edit/write handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bareagent.core.handlers.file_edit import run_edit
from bareagent.core.handlers.file_write import run_write
from bareagent.core.tools import _build_diagnostics_hook
from bareagent.lsp.config import LSPConfig

# ---------------------------------------------------------------------------
# Hook factory: config gate + happy path
# ---------------------------------------------------------------------------


class _StubManager:
    def __init__(
        self,
        cfg: LSPConfig,
        rows_after_edit: list[dict[str, Any]] | None = None,
        *,
        language: str | None = "python",
    ) -> None:
        self.config = cfg
        self._language = language
        self._rows: list[dict[str, Any]] = rows_after_edit or []
        self.repository_root = "."

    def language_for_file(self, path: str) -> str | None:
        return self._language

    def get_server_for_file(self, path: str) -> Any:
        return object() if self._language else None

    def get_diagnostics_snapshot(self, path: str) -> list[dict[str, Any]]:
        return list(self._rows)

    def wait_for_diagnostics(self, path: str, timeout: float = 1.5) -> bool:
        return True


def test_hook_is_none_when_no_manager() -> None:
    assert _build_diagnostics_hook(None) is None


def test_hook_returns_none_when_flag_off() -> None:
    mgr = _StubManager(LSPConfig(auto_diagnostics_on_edit=False))
    hook = _build_diagnostics_hook(mgr)  # type: ignore[arg-type]
    assert hook is not None
    # ``before=None`` is the pre-edit snapshot path.
    assert hook("a.py", None) is None


def test_hook_snapshot_then_diff_happy_path() -> None:
    rows = [
        {
            "severity": 1,
            "message": "boom",
            "source": "pyright",
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
        }
    ]
    mgr = _StubManager(LSPConfig(auto_diagnostics_on_edit=True), rows_after_edit=rows)
    hook = _build_diagnostics_hook(mgr)  # type: ignore[arg-type]
    assert hook is not None
    before = hook("a.py", None)  # snapshot before — empty since rows only added on diff?
    # Our stub returns the same rows pre/post; the diff should be empty.
    appendix = hook("a.py", before)
    assert appendix is None


def test_hook_returns_appendix_when_new_diag_appears() -> None:
    cfg = LSPConfig(auto_diagnostics_on_edit=True)
    # Pre-edit cache is empty; post-edit it has one new row. We model this
    # by mutating the stub between calls.
    mgr = _StubManager(cfg, rows_after_edit=[])
    hook = _build_diagnostics_hook(mgr)  # type: ignore[arg-type]
    assert hook is not None
    before = hook("a.py", None)
    mgr._rows = [
        {
            "severity": 1,
            "message": "new boom",
            "source": "pyright",
            "range": {
                "start": {"line": 4, "character": 0},
                "end": {"line": 4, "character": 1},
            },
        }
    ]
    appendix = hook("a.py", before)
    assert appendix is not None
    assert "Newly introduced diagnostics in a.py:" in appendix
    assert "[pyright Error] Line 5:1 — new boom" in appendix


# ---------------------------------------------------------------------------
# edit_file integration: hook called pre + post; appendix appended only when
# the hook returns non-None.
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    sample = tmp_path / "foo.py"
    sample.write_text("x = 1\n")
    return tmp_path


def test_edit_file_appends_when_hook_returns_appendix(workspace: Path) -> None:
    calls: list[Any] = []

    def _hook(path: str, before: Any) -> Any:
        calls.append(before)
        if before is None:
            return ["snapshot-token"]  # opaque "before" payload
        return "\n\nNewly introduced diagnostics in foo.py:\n- [pyright Error] Line 1:1 — bad"

    result = run_edit(
        "foo.py",
        "x = 1",
        "x: int = 'string'",
        workspace=workspace,
        diagnostics_hook=_hook,
    )
    # Pre-call passed ``None``, post-call received the snapshot payload.
    assert calls[0] is None
    assert calls[1] == ["snapshot-token"]
    assert "Newly introduced diagnostics in foo.py:" in result
    # File still got edited.
    assert (workspace / "foo.py").read_text(encoding="utf-8") == "x: int = 'string'\n"


def test_edit_file_skips_appendix_when_hook_returns_none(workspace: Path) -> None:
    def _hook(path: str, before: Any) -> Any:
        return None

    result = run_edit(
        "foo.py",
        "x = 1",
        "x = 2",
        workspace=workspace,
        diagnostics_hook=_hook,
    )
    # Plain "Edited foo.py" — no appendix.
    assert "Newly introduced diagnostics" not in result
    assert result.startswith("Edited")


def test_edit_file_works_without_hook(workspace: Path) -> None:
    result = run_edit("foo.py", "x = 1", "x = 2", workspace=workspace)
    assert result.startswith("Edited")
    assert "Newly introduced diagnostics" not in result


# ---------------------------------------------------------------------------
# write_file integration — same contract as edit_file
# ---------------------------------------------------------------------------


def test_write_file_appends_appendix(workspace: Path) -> None:
    def _hook(path: str, before: Any) -> Any:
        if before is None:
            return []
        return (
            "\n\nNewly introduced diagnostics in new.py:\n"
            "- [pyright Error] Line 2:1 — missing return"
        )

    result = run_write(
        "new.py",
        "def foo():\n  pass\n",
        workspace=workspace,
        diagnostics_hook=_hook,
    )
    assert "Newly introduced diagnostics in new.py:" in result
    assert (workspace / "new.py").exists()


def test_write_file_works_without_hook(workspace: Path) -> None:
    result = run_write(
        "new.py",
        "x = 1\n",
        workspace=workspace,
    )
    assert result.startswith("Wrote")
    assert "Newly introduced diagnostics" not in result
