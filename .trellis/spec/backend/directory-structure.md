# Directory Structure

> How BareAgent's `src/` is layered and where new code belongs.

BareAgent is a **pure Python terminal agent**, not a web service. There are no routes, controllers, or ORMs — instead the codebase is sliced into runtime concerns (LLM loop, providers, permissions, planning, team coordination). Pick the package by **what the code does at runtime**, not by an OOP layer.

---

## Top-level layout

```
src/
├── main.py            # Entry point, CLI parsing, config loading, REPL loop
├── core/              # The agent loop, tool registry, schemas, sandbox
│   ├── loop.py        #   agent_loop() — LLM → tool calls → permission → handler → result
│   ├── tools.py       #   BASE_TOOLS + DEFERRED_TOOLS registry
│   ├── schema.py      #   tool_schema() helper
│   ├── sandbox.py     #   safe_path() — workspace-relative path enforcement
│   ├── fileutil.py    #   stringify, atomic_write_json, utc_timestamp_iso, …
│   └── handlers/      #   One file per tool handler (bash.py, file_read.py, …)
├── provider/          # LLM provider abstraction
│   ├── base.py        #   BaseLLMProvider ABC + LLMResponse / ToolCall / StreamEvent
│   ├── anthropic.py   #   Anthropic implementation
│   ├── openai.py      #   OpenAI + DeepSeek (OpenAI-compatible) implementation
│   └── factory.py     #   create_provider(config) entry point
├── permission/        # PermissionGuard — 4 modes, allow/deny rules, danger patterns
├── planning/          # tasks.py, todo.py, skills.py, subagent.py, agent_types.py
├── team/              # Multi-agent: mailbox.py, autonomous.py, manager.py, protocols.py
├── memory/            # compact.py, transcript.py, token_counter.py
├── concurrency/       # background.py (thread pool), notification.py
├── tracing/           # Tracer ABC + proxy + JsonFile / Langfuse / OTel backends
├── debug/             # InteractionLogger, web_viewer.py SPA
└── ui/                # AgentConsole (rich), StreamPrinter, prompt.py (prompt-toolkit), theme.py
```

**Rule**: every subpackage owns one runtime concern. Never reach into another package's private helpers (names prefixed with `_`); use the public surface only.

---

## Where new code goes (decision tree)

Use this in order — stop at the first match:

1. **A new tool the LLM can call?** → handler in `src/core/handlers/<tool_name>.py`, schema in `src/core/schema.py` registration, register in `src/core/tools.py` (`BASE_TOOLS` for always-on, `DEFERRED_TOOLS` for lazy-loaded).
2. **A new LLM provider?** → `src/provider/<name>.py` implementing `BaseLLMProvider.create()` and `create_stream()`; wire it into `src/provider/factory.py`.
3. **A new safety check / permission rule?** → extend `src/permission/guard.py`. Danger patterns go into `PermissionGuard.DANGEROUS_PATTERNS`, auto-safe ones into `AUTO_SAFE_PATTERNS`.
4. **Sub-agent / task / TODO / skill behavior?** → `src/planning/`. Pick `tasks.py` (persistent), `todo.py` (session-scoped), `skills.py` (markdown-driven), `subagent.py` (delegation), or `agent_types.py` (type definitions).
5. **Inter-agent messaging or daemonized teammate?** → `src/team/`. Mailbox primitives in `mailbox.py`, protocol FSMs in `protocols.py`, lifecycle in `autonomous.py` + `manager.py`.
6. **Token counting, context compaction, transcript persistence?** → `src/memory/`.
7. **Threading / async / completion notifications?** → `src/concurrency/`. There is no asyncio; all background work goes through `BackgroundManager` (daemon threads + completion queue).
8. **Observability span / metric?** → `src/tracing/`. Add a new backend file if needed; otherwise just call `tracer.trace(...)` at the instrumentation site.
9. **Debug payload capture / inspector UI?** → `src/debug/`.
10. **Terminal rendering / input?** → `src/ui/`. **Never** print directly from non-ui packages.
11. **REPL command, config plumbing, top-level wiring?** → `src/main.py`.

If a change touches more than one of the above, decompose it. A handler that needs persistence calls into `planning/`; it does not put a JSON file alongside itself.

---

## Module boundaries (do not cross these)

- `core/loop.py` is the only place that knows the LLM → tool → permission sequence. Tool handlers must not call `agent_loop()` themselves (sub-agents go through `planning/subagent.py`).
- `provider/` knows about wire formats and SDKs; it returns normalized `LLMResponse` and never imports from `core/` or `permission/`.
- `permission/guard.py` is the single source of truth for "is this allowed". Handlers must not re-implement safety checks. Example: `core/handlers/bash.py` runs the command and reports its output; it does not inspect the command for `rm -rf`. `PermissionGuard.DANGEROUS_PATTERNS` does that before the handler is invoked.
- `ui/` depends on no other runtime package. Tools, providers, and the loop accept a `UIProtocol` (`src/ui/protocol.py`) — they never import `AgentConsole` directly.

---

## Naming conventions

- Files are `snake_case.py`; classes are `PascalCase`; functions are `snake_case`. Single-underscore prefix marks intra-module helpers (e.g. `_validate_agent_name` in `src/team/mailbox.py`).
- Public dataclasses live next to the manager that owns them (e.g. `Task` in `tasks.py`, `Message` in `mailbox.py`, `LLMResponse` in `provider/base.py`).
- Hidden state directories follow the `.<name>/` convention at the workspace root: `.transcripts/`, `.mailbox/<session>/`, `.logs/<session>/`, `.tasks.json`. **Why**: keeps generated state out of source listings and easy to add to `.gitignore`.

---

## Tests

- One test file per module: `tests/test_<module>.py` mirrors `src/<package>/<module>.py`.
  - Example: `src/team/mailbox.py` → `tests/test_team.py` (grouped by package when modules are tightly coupled) or `tests/test_mailbox_manual.py` (manual smoke tests).
- Pure unit tests run in default `pytest`; integration tests that require API keys or interactive input are suffixed `_manual.py` and excluded from CI defaults.
- Shared fixtures live in `tests/conftest.py`. `make_test_config(tmp_path)` is the canonical way to build a `Config` for tests — use it instead of constructing `Config` inline.

---

## Anti-patterns (do not do)

- Do **not** create a new top-level package without a clear runtime concern. If the new code is fewer than ~200 lines, find an existing package.
- Do **not** put business logic in `src/main.py`. `main.py` is wiring (config → managers → loop). Anything reusable belongs in a package.
- Do **not** add `utils.py` grab-bags. Helpers live next to their owner (`fileutil.py` is the only shared utility module, and it stays small).
