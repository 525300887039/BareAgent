#!/usr/bin/env bash
# Run the exact checks CI runs, in CI's order, via `uv run` so the local
# venv/sys.path matches CI. Using `python -m pytest` here would prepend the cwd
# to sys.path and mask import failures that only surface under CI's bare
# `uv run pytest` -- the exact gap that kept main red for a week in June 2026.
#
# Reused by the .githooks/pre-push gate and runnable by hand:  bash scripts/ci-check.sh
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    echo "[ci-check] error: 'uv' not found on PATH." >&2
    echo "[ci-check] Install uv (https://docs.astral.sh/uv/) or set BAREAGENT_PREPUSH_SKIP=1 to bypass the hook." >&2
    exit 127
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

echo "[ci-check] (1/4) uv run ruff check src tests"
uv run ruff check src tests

echo "[ci-check] (2/4) uv run ruff format --check src tests"
uv run ruff format --check src tests

echo "[ci-check] (3/4) uv run pyright"
uv run pyright

echo "[ci-check] (4/4) uv run pytest"
uv run pytest

echo "[ci-check] OK -- CI-equivalent checks passed."
