"""Guard the CI-visibility setup against regression.

Two kinds of checks:
1. Unit tests for the pure ``decide_action`` decision table in ``scripts/ci_notify.py``.
2. Static assertions that the CI config keeps the properties that prevent the
   June 2026 "main red for a week" incident from recurring -- chiefly that CI and
   the local gate run the faithful ``uv run pytest`` form (never ``python -m pytest``,
   which prepends cwd to sys.path and masks import failures).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_notify import Action, combine_conclusions, decide_action

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- decide_action decision table -------------------------------------------------


@pytest.mark.parametrize(
    ("open_count", "conclusion", "expected"),
    [
        (0, "failure", Action.CREATE),
        (1, "failure", Action.COMMENT),
        (3, "failure", Action.COMMENT),
        (0, "success", Action.NOOP),
        (1, "success", Action.CLOSE),
        (2, "success", Action.CLOSE),
        (0, "cancelled", Action.NOOP),
        (1, "cancelled", Action.NOOP),
        (1, "skipped", Action.NOOP),
        (0, "weird-unknown", Action.NOOP),
        (1, "", Action.NOOP),
    ],
)
def test_decide_action_table(open_count: int, conclusion: str, expected: Action) -> None:
    assert decide_action(open_count, conclusion) is expected


# --- combine_conclusions: reduce multiple job results to one signal ----------------


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (["success", "success"], "success"),  # all green -> green
        (["success", "failure"], "failure"),  # any red -> red
        (["failure", "success"], "failure"),
        (["failure", "failure"], "failure"),
        (["success", "cancelled"], ""),  # ambiguous -> no signal
        (["success", "skipped"], ""),
        (["cancelled", "skipped"], ""),
        ([], ""),  # nothing -> no signal
        (["success"], "success"),
        (["failure"], "failure"),
    ],
)
def test_combine_conclusions(results: list[str], expected: str) -> None:
    assert combine_conclusions(results) == expected


# --- static anti-regression guards ------------------------------------------------


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _command_lines(text: str) -> str:
    """Drop comment lines so guards check what runs, not explanatory prose.

    Both YAML and shell use ``#`` for comments; the scripts intentionally *mention*
    ``python -m pytest`` in a comment explaining why it is forbidden.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_ci_workflow_runs_faithful_uv_run_pytest() -> None:
    ci = _read(".github/workflows/ci.yml")
    assert "uv run pytest" in ci
    # `python -m pytest` prepends cwd to sys.path and would re-mask the import bug.
    assert "python -m pytest" not in _command_lines(ci)


def test_ci_check_script_runs_faithful_uv_run_pytest() -> None:
    script = _read("scripts/ci-check.sh")
    assert "uv run pytest" in script
    assert "uv run ruff check src tests" in script
    assert "python -m pytest" not in _command_lines(script)


def test_ci_workflow_has_notify_job() -> None:
    ci = _read(".github/workflows/ci.yml")
    assert "notify:" in ci
    assert "issues: write" in ci
    assert "ci_notify.py" in ci
    assert "refs/heads/main" in ci


def test_ci_workflow_has_socket_job() -> None:
    ci = _read(".github/workflows/ci.yml")
    assert "socket:" in ci
    # The socket job's whole point: run the otherwise-zero-coverage socket suite.
    assert "uv run pytest -m socket" in ci


def test_notify_depends_on_socket_job() -> None:
    # Socket-job failures on main must also trigger the ci-failure issue; if notify
    # only watched `test`, a socket regression would redden CI with no tracking issue.
    ci = _read(".github/workflows/ci.yml")
    assert "needs: [test, socket]" in ci
    assert "socket:${{ needs.socket.result }}" in ci


def test_socket_marker_registered() -> None:
    # `-m socket` selection relies on the marker being declared (no strict-marker churn).
    pyproject = _read("pyproject.toml")
    assert '"socket:' in pyproject


def test_ci_test_job_covers_windows() -> None:
    # CI must keep testing the Windows dev platform, not just Linux; fail-fast off
    # so a Windows-only failure doesn't mask/cancel the Linux result.
    ci = _read(".github/workflows/ci.yml")
    assert "windows-latest" in ci
    assert "ubuntu-latest" in ci
    assert "fail-fast: false" in ci


def test_ruff_pinned_exact() -> None:
    # Pinning ruff exact keeps `ruff check`/`format` reproducible: a new release can
    # add lint rules (surprise CI red) or reflow code (whole-tree format churn).
    pyproject = _read("pyproject.toml")
    assert "ruff==" in pyproject
    assert "ruff>=" not in pyproject


def test_format_check_enforced_in_ci_and_local_gate() -> None:
    # Bulk-reformat only "sticks" if both CI and the pre-push gate reject drift.
    ci = _read(".github/workflows/ci.yml")
    gate = _read("scripts/ci-check.sh")
    assert "ruff format --check src tests" in ci
    assert "ruff format --check src tests" in gate


def test_pre_push_hook_present_and_wired() -> None:
    hook = _read(".githooks/pre-push")
    assert "scripts/ci-check.sh" in hook
    # Bypass knob must stay documented in the hook itself.
    assert "BAREAGENT_PREPUSH_SKIP" in hook


def test_pyproject_keeps_pythonpath_root() -> None:
    # The actual root-cause fix: without this, `uv run pytest` can't import tests.conftest.
    pyproject = _read("pyproject.toml")
    assert 'pythonpath = ["."]' in pyproject


def test_ci_workflow_runs_pyright() -> None:
    # pyright is configured in [tool.pyright] but must actually run in CI; otherwise
    # it's a "configured but never enforced" gate -- the failure mode this task closed.
    ci = _read(".github/workflows/ci.yml")
    assert "uv run pyright" in ci


def test_ci_check_script_runs_pyright() -> None:
    # The pre-push gate must run pyright too, so type errors are caught before push,
    # keeping the local gate faithful to CI (same as ruff / format-check / pytest).
    script = _read("scripts/ci-check.sh")
    assert "uv run pyright" in script


def test_pyright_pinned_exact() -> None:
    # Exact pin keeps type results reproducible: a new pyright release can surface
    # new errors (surprise CI red). The PyPI package is a wrapper that downloads the
    # matching node pyright, so pinning the package pins the checker version.
    pyproject = _read("pyproject.toml")
    assert "pyright==" in pyproject
    assert "pyright>=" not in pyproject


def test_pyright_standard_mode() -> None:
    # The type gate is tightened to `standard`; guard against a silent revert to
    # `basic`, which would stop enforcing the stricter override / optional checks.
    pyproject = _read("pyproject.toml")
    assert 'typeCheckingMode = "standard"' in pyproject
