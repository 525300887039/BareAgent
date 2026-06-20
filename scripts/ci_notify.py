#!/usr/bin/env python3
"""Open / update / close a single "main is red" tracking issue from CI.

Called by the ``notify`` job in ``.github/workflows/ci.yml`` after the ``test``
job finishes on a push to ``main``. The decision of *what* to do is a pure
function (``decide_action``) so it is unit-tested without touching GitHub; the
thin ``main`` shells out to the ``gh`` CLI to carry the decision out.

Lifecycle (deduped by a fixed label so we never pile up issues):
- test failed, no open issue  -> CREATE a new tracking issue
- test failed, issue is open   -> COMMENT on it (main went red again)
- test passed, issue is open   -> CLOSE it (recovered)
- test passed, no open issue   -> NOOP
- cancelled / skipped / other  -> NOOP (not an actionable signal)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from enum import StrEnum

DEFAULT_LABEL = "ci-failure"
DEFAULT_TITLE = "CI failing on main"


class Action(StrEnum):
    CREATE = "create"
    COMMENT = "comment"
    CLOSE = "close"
    NOOP = "noop"


def decide_action(open_issue_count: int, conclusion: str) -> Action:
    """Map (number of open tracking issues, test job conclusion) to an action.

    Pure and side-effect free so the full decision table is unit-testable.
    ``conclusion`` is the GitHub ``needs.test.result`` value
    (``success`` / ``failure`` / ``cancelled`` / ``skipped``).
    """
    has_open = open_issue_count > 0
    if conclusion == "failure":
        return Action.COMMENT if has_open else Action.CREATE
    if conclusion == "success":
        return Action.CLOSE if has_open else Action.NOOP
    # cancelled / skipped / unknown: not a clean red or green, do nothing.
    return Action.NOOP


def _run_gh(args: list[str]) -> str:
    """Run a ``gh`` subcommand and return stdout (text)."""
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _list_open_issue_numbers(label: str) -> list[int]:
    raw = _run_gh(
        ["issue", "list", "--label", label, "--state", "open", "--json", "number"]
    )
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [int(item["number"]) for item in items if "number" in item]


def _ensure_label(label: str) -> None:
    # Idempotent: --force updates an existing label instead of erroring, and a
    # fresh repo may not have the label yet (gh issue create requires it to exist).
    try:
        _run_gh(
            ["label", "create", label, "--color", "B60205",
             "--description", "Tracks CI failures on main", "--force"]
        )
    except subprocess.CalledProcessError:
        # Best-effort: if label creation fails we still try to create the issue;
        # a missing label only matters for the CREATE path and gh will report it.
        pass


def _build_failure_body(sha: str, run_url: str) -> str:
    return (
        f"CI failed on `main` at commit `{sha}`.\n\n"
        f"Run: {run_url}\n\n"
        "This issue is auto-managed by `scripts/ci_notify.py`: it is reused for "
        "repeat failures and auto-closed once CI passes on `main` again."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conclusion", required=True, help="needs.test.result value")
    parser.add_argument("--sha", default="", help="commit SHA that ran")
    parser.add_argument("--run-url", default="", help="URL of the failing run")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    args = parser.parse_args(argv)

    open_numbers = _list_open_issue_numbers(args.label)
    action = decide_action(len(open_numbers), args.conclusion)
    print(f"[ci-notify] conclusion={args.conclusion} open={len(open_numbers)} -> {action.value}")

    if action is Action.NOOP:
        return 0

    if action is Action.CREATE:
        _ensure_label(args.label)
        _run_gh(
            ["issue", "create", "--title", args.title, "--label", args.label,
             "--body", _build_failure_body(args.sha, args.run_url)]
        )
        return 0

    # COMMENT and CLOSE both act on the existing open issue(s).
    for number in open_numbers:
        if action is Action.COMMENT:
            body = f"CI went red again on `main` at `{args.sha}`.\n\nRun: {args.run_url}"
            _run_gh(["issue", "comment", str(number), "--body", body])
        elif action is Action.CLOSE:
            body = f"CI is green again on `main` at `{args.sha}`. Closing.\n\nRun: {args.run_url}"
            _run_gh(["issue", "comment", str(number), "--body", body])
            _run_gh(["issue", "close", str(number)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
