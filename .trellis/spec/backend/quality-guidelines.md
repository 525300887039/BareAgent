# Quality Guidelines

> Code standards, testing, and commit conventions for BareAgent.

These are not aspirations — they are the rules the existing `src/` and `tests/` follow. Sub-agents and reviewers should enforce them.

---

## Python 3.12+ features and stdlib first

The project targets `requires-python = ">=3.12"` (`pyproject.toml`). Use modern features:

- `from __future__ import annotations` at the top of every module (universal in `src/`).
- `X | None` instead of `Optional[X]`; `list[X]` / `dict[K, V]` instead of `List` / `Dict`.
- `match`/`case` for tagged-union dispatch where it improves clarity.
- `tomllib` (stdlib) instead of `tomli` / `tomlkit`. See `src/main.py`.
- `dataclasses` with `slots=True` for value objects (`Task`, `Message`, `LLMResponse`, `Config`, …). Use `frozen=True` for immutable definitions like `AgentType`.

**Why**: every external dependency is a supply-chain risk and a Windows-install footgun. The stdlib already covers TOML parsing, threading, JSON, subprocess, pathlib, and dataclasses.

Dependencies are gated to four runtime libraries (`anthropic`, `openai`, `prompt-toolkit`, `rich`) plus optional `langfuse` / `opentelemetry-*` extras. Adding a new dependency requires justification in the PR.

---

## Type annotations everywhere

Annotate every public function signature and every non-trivial variable. The codebase uses `Any` sparingly and only at provider boundaries where the upstream SDK is untyped. Example — `src/team/mailbox.py`:

```python
def receive(self, agent_name: str, since_id: str | None = None) -> list[Message]:
    ...
```

Forward references for cycle-breaking go through `TYPE_CHECKING`:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.planning.agent_types import AgentType
```

**Rule**: no untyped `def foo(x, y):` in new code. `# type: ignore[…]` comments need a specific error code and a short reason.

---

## `ruff` is the only lint/format tool

```bash
ruff check src tests       # lint
ruff check --fix src tests  # autofix
ruff format src tests       # format
```

No `black`, no `isort`, no `flake8`, no `mypy` (yet — pre-commit hooks may add it later). Ruff's defaults are the project defaults.

**Rule**: a PR must pass `ruff check src tests` **and** `ruff format --check src tests` cleanly. Fix lints; don't add `# noqa` unless the rule is genuinely wrong for the context (then comment why).

### ruff is pinned exact — whole-tree `ruff format` is now safe

`ruff` is pinned **exact** (`ruff==0.15.8` in `pyproject.toml`'s `dev` extra) and the whole tree was reformatted with it (task 06-21, the ruff-pin cleanup). So everyone — local dev, the pre-push gate, and CI — runs the same ruff, and `ruff format src tests` is now a **no-op on an unchanged tree** rather than a churn generator. CI and `scripts/ci-check.sh` both run `ruff format --check src tests`, so any drift is rejected before it lands.

**Rule**: just run `ruff format src tests` (whole tree) before committing — it only touches what you changed. **Bumping ruff** (e.g. to 0.16.x) is a deliberate, standalone PR: change the `==` pin, run `ruff format src tests`, and commit the resulting reformat together with the bump — never let a new ruff version reach the tree without reformatting in the same change. (Historical note: this rule used to be the opposite — "never `ruff format src tests`" — back when ruff was pinned loosely (`>=0.11`) and the tree lagged a newer ruff; pinning exact + a one-time bulk reformat retired that footgun.)

---

## Tests live in `tests/test_<module>.py`

Every behavior change ships with a pytest test. Layout:

- `tests/test_<module>.py` for unit tests that run in CI defaults (`test_loop.py`, `test_tools.py`, `test_compact.py`, …).
- `tests/test_<module>_manual.py` for tests requiring API keys, interactive input, or real subprocess execution that's flaky in CI (`test_provider_manual.py`, `test_subagent_manual.py`).
- `tests/conftest.py` holds shared fixtures. `make_test_config(tmp_path)` builds a `Config` for the loop/REPL — use it instead of constructing `Config` inline.

Test function names spell out the behavior: `test_agent_loop_streams_text_chunks`, `test_permission_guard_blocks_rm_rf`. Not `test_loop_1`, not `test_basic`.

**Run targets**:

```bash
pytest                                # everything except manual smoke tests
pytest tests/test_loop.py             # one module
pytest tests/test_loop.py -k "stream" # by name substring
```

**Rule**: a new public function or branch needs a unit test in the matching module. A bug fix needs a regression test that fails without the fix.

---

## Conventional Commits (with the project's exact casing)

Commit subject format observed in `git log`:

```
Feat: 新增 web_fetch 和 web_search 工具
Refactor: 移除 Textual TUI，切换为 prompt-toolkit 终端交互
Fix: ...
Test: ...
Docs: ...
Chore: ...
```

Note the **capitalized prefix with colon-space** (`Feat: `, not `feat:`). Subjects can be Chinese or English; body text follows the same rule.

Prefix selection:

- `Feat:` — user-visible new behavior.
- `Fix:` — bug fix.
- `Refactor:` — internal restructure with no behavior change.
- `Test:` — test-only additions/changes.
- `Docs:` — documentation, including `CLAUDE.md`, `README.md`, and `.trellis/spec/`.
- `Chore:` — tooling, dependencies, config, `.gitignore`.

**Rule**: no `wip`, no `update`, no `misc fixes`. If a commit spans two prefixes, split it.

---

## Avoid over-engineering

The project consciously avoids speculative abstraction. Concrete rules:

- **No interfaces / Protocols / ABCs without two or more implementations.** `BaseLLMProvider` is justified (Anthropic + OpenAI). `UIProtocol` is justified (`AgentConsole` + `FakeConsole` in tests). A solo abstract class is dead weight.
- **No "manager of managers" wrappers.** Each manager (`TaskManager`, `TodoManager`, `TeammateManager`, `MessageBus`, `BackgroundManager`) owns one concern and is wired together in `src/main.py`. Don't wrap them in a single facade.
- **Comments explain why, not what.** The code shows what; comments add the rationale or non-obvious constraint. Example from `src/concurrency/background.py`:

  ```python
  # Prune dead threads to prevent unbounded growth.
  self._threads = {tid: t for tid, t in self._threads.items() if t.is_alive()}
  ```

  Not `# Filter the threads dict`.

- **Delete dead code.** Helper functions with zero callers get removed in the same PR, not "kept for future use".

---

## What sub-agents must not do

- **Do not write markdown documentation files** (READMEs, design notes, summaries) unless the user explicitly asks. The project keeps docs in `docs/` (VitePress) and a few well-known top-level files (`CLAUDE.md`, `BAREAGENT.md`, `README.md`, `ROADMAP.md`). Drive-by `.md` files clutter the tree.
- **Do not use emojis in source files** unless the user explicitly requests them. Theme icons (✓, ⚠, etc.) live in `src/ui/theme.py` and are routed through `AgentConsole`; new emojis in code or comments are out.
- **Do not `git commit` from inside a sub-agent.** The bootstrap PRD makes this explicit; the main session handles commits after reviewing the diff.
- **Do not introduce framework-shaped patterns** (decorators that hide control flow, dynamic plugin registries, metaclasses) when a plain function or dict-of-callables works. The tool registry in `src/core/tools.py` is the project's high-water mark for indirection — that's the ceiling, not the floor.
