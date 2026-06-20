#!/usr/bin/env bash
# One-time setup: point git at the committed .githooks/ directory so the pre-push
# gate activates. Committed hooks do not auto-activate (git only runs
# .git/hooks/), so every fresh clone runs this once.
#
# PowerShell users without bash can run the single config line directly:
#   git config core.hooksPath .githooks
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config core.hooksPath .githooks

echo "[setup-hooks] core.hooksPath -> .githooks"
echo "[setup-hooks] pre-push now runs scripts/ci-check.sh before each push."
echo "[setup-hooks] Bypass once with: BAREAGENT_PREPUSH_SKIP=1 git push   (or git push --no-verify)"
