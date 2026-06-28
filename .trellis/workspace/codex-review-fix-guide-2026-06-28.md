# Codex Review Fix Guide - 2026-06-28

## Inputs

- Source report: `.trellis/workspace/codex-review-2026-06-28.md`
- Raw review evidence: `C:\Users\no525\AppData\Local\Temp\bareagent-codex-review-20260628`
- Coverage in source report: 212 commits reviewed one by one, plus current uncommitted worktree diff
- Current worktree at guide time: existing uncommitted Trellis platform changes, plus the review report file

This guide intentionally treats the review as current-state maintenance work. Do not rewrite old commits to "fix" historical review findings. Fix the current `main` state with new, focused commits.

## Priority Model

- P0: current dirty worktree inconsistency that should not be committed as-is.
- P1: security, privacy, data loss, broken runtime behavior, or release/install breakage.
- P2: user-facing correctness, stale cache behavior, missing propagation, incomplete config semantics.
- P3: docs, Trellis archive hygiene, journal completeness, and historical audit cleanup.

## Batch 0 - Resolve Current Dirty Trellis Platform Diff

Source findings: uncommitted review findings in the source report.

Decision first:

- Option A: Trae-only support. Remove `ZCode` and `Reasonix` from `.trellis/workflow.md` wherever they imply supported dispatch platforms, and add missing `Trae` to the Phase 1.3 top-level platform list.
- Option B: full Trae + ZCode + Reasonix support. Add `zcode` and `reasonix` everywhere platform support is implemented, not only in workflow prose.

If choosing Option B, update at least:

- `.trellis/scripts/common/cli_adapter.py`: `Platform` literal, config directory mapping, CLI/resume behavior, `get_cli_adapter()`, `detect_platform()`, `_ALL_PLATFORM_CONFIG_DIRS`, env override whitelist.
- `.trellis/scripts/common/task_store.py`: seed `implement.jsonl` / `check.jsonl` for `.zcode` and `.reasonix` config directories.
- `.trellis/scripts/common/active_task.py`: session env keys if those platforms have session identifiers.
- `.trellis/workflow.md`: make every platform list consistent, including Trae in Phase 1.3.
- `.trellis/.template-hashes.json` and `.trellis/.version`: keep only if they are expected output from a Trellis template update.

Validation:

- `python ./.trellis/scripts/task.py current --source`
- `python ./.trellis/scripts/get_context.py --mode phase --step 1.3`
- Add or run existing Trellis script tests if this repo has them for `detect_platform`, JSONL seeding, and active task session resolution.

## Batch 1 - Permission And Network Safety

Source findings: `66651e7`, `bce309c`, `39804e5`.

Current-state evidence:

- `src/bareagent/permission/guard.py` still includes `web_fetch` and `web_search` in `SAFE_TOOLS`.
- `edit_file`, `task_create`, and `task_update` still return `False` from `requires_confirm()`.
- `background_run` is a shell-command tool, but permission subject/danger matching is not clearly equivalent to `bash`.

Fix guide:

- Remove arbitrary network tools from unconditional safe status, or split them into a separate network permission policy. In DEFAULT mode, prompt before arbitrary outbound fetch/search. In PLAN mode, be explicit whether web read is allowed; if allowed, document why and block localhost/private network SSRF-style targets.
- Treat all mutating tools as confirmation-required unless mode is BYPASS. This includes `edit_file`, `write_file`, `semantic_rename`, `task_create`, `task_update`, and any task/memory operation that can overwrite existing durable state.
- Make `fail_closed=True` force confirmation/denial for mutating tools even if a previous branch would mark them safe.
- Make `background_run` use the command string as its rule subject and run the same dangerous-pattern checks as `bash`.
- Review `memory` safe status separately. At minimum, `memory.create()` must not overwrite existing memory files without an explicit replace operation.

Regression tests:

- DEFAULT mode prompts for `edit_file`, `write_file`, `task_create`, `task_update`.
- `fail_closed=True` blocks or prompts mutating tools without user input.
- `background_run` with `rm -rf`, shell wrappers, and allow/deny prefixes behaves like `bash`.
- `web_fetch` to localhost/private-network examples does not silently run in DEFAULT unless that is an explicit accepted policy.

## Batch 2 - Tracing, Privacy, And Debug Logging

Source findings: `57a8dc0`, `82c4bcb`, `79a9638`, `0f93348`, `9331115`, plus docs findings around tracing.

Current-state evidence:

- `ProxyTracer.is_content_tracing_enabled` is stored but not used to gate `Span.set_content_tag()`.
- `_configure_tracing()` runs at startup, while `/new`, `/resume`, `/fork`, and `/import` update compact/logger session IDs but do not obviously update external tracing sessions.
- Langfuse/OTel expose `flush()` / `shutdown()`, but normal REPL exit paths need an explicit finalizer.

Fix guide:

- Add a redacting span wrapper or proxy-layer guard so `content_enabled=false` and `BAREAGENT_CONTENT_TRACING_ENABLED=false` actually prevent prompts, tool inputs, tool outputs, and LLM outputs from reaching external backends.
- Thread TOML `[tracing] content_enabled` into the global proxy instead of relying on environment-only initialization.
- Add a single helper for session switches that updates compact session, interaction logger, and tracing backend session together. Use it in `/new`, `/resume`, `/fork`, and `/import`.
- Wrap stdio/Textual app lifecycles in `try/finally` and call `global_tracer.shutdown()` or at least `global_tracer.flush()` on normal exit, EOF, and KeyboardInterrupt.
- Pass debug logging into subagent and autonomous-agent `agent_loop` calls, or document a deliberate scope boundary. If logging claims "full LLM request/response log", it must include delegated loops.
- Make debug log writes atomic. Do not use direct `write_text()` for JSON files read concurrently by the viewer.
- Fix cached-token accounting in interaction logs and tracing so total input and cached input are both represented.

Regression tests:

- With `content_enabled=false`, fake tracing backend receives no content tags.
- After `/resume` or `/new`, fake Langfuse tracer sees the new session ID.
- Exit path calls `flush()` / `shutdown()` on a fake tracer.
- Debug logging captures a subagent or team-agent LLM call when debug logging is enabled.
- Corrupted/interrupted log write cannot leave partial JSON visible to the viewer.

## Batch 3 - Provider Streaming, Retry, And Terminal Rendering

Source findings: `6d9b1ba`, `2046332`, `08333ca`, `f93a2b6`, `6805128`, `4a5b328`, `d8f1067`, `49cd034`, `cbfcc66`.

Fix guide:

- In `src/bareagent/provider/openai.py`, merge streamed and final Responses tool calls by tool-call id. Do not use streamed calls only when the final payload has zero tool calls; append streamed-only calls missing from the final payload.
- For streaming retries, avoid retrying after user-visible text/tool previews have been emitted, or buffer stream output until an attempt is known to be successful. Do not leave stale partial output on screen after retry.
- Validate retry config at parse time: `base_delay_sec >= 0`, `max_delay_sec >= 0`, `multiplier >= 1`, and a sane relation between base/max. Invalid config should fall back with a warning or raise a config error, but must not mask the original LLM failure via `time.sleep()`.
- Make `StreamPrinter.finish()` idempotent and make theme push/pop scoped to each active stream segment. It must support `text -> tool_call -> text` in one stream without losing or over-popping theme state.
- Restore ordered tool-result labeling in chat history. Do not precompute a last-write-wins tool-call-id map across the whole transcript when repeated fallback IDs are possible.
- Restore defensive schema isolation in `get_tools()`, or make tool schema objects immutable. A caller must not be able to mutate global schema state through a returned list.
- Separate "tool display stringification" from "memory/compaction serialization" so `None` remains distinguishable from empty text where memory summaries need JSON semantics.
- Verify Anthropic cache TTL behavior: `ttl="1h"` must include the required beta/API support or be rejected before request.
- Verify prompt-cache anchor selection only reports a breakpoint when one can actually be placed.

Regression tests:

- Responses stream emits `call_1` and `call_2`; final payload contains only `call_1`; merged result contains both.
- Streaming failure after first text event does not duplicate or contradict visible output.
- Negative retry delays are rejected or clamped.
- Stream sequence `text`, `tool_call`, `text` preserves theme and does not pop twice.
- Mutating `get_tools()[0]["parameters"]` does not affect a later `get_tools()` call.
- Compaction preserves explicit `null` where old behavior did.

## Batch 4 - Repo Map And Code Search

Source findings: `6f87b77`, `78e4f50`, `dbcbaa9`, `faa588c`, `92534f0`, `826d25a`.

Current-state evidence:

- `_team_spawn()` builds teammate handlers without passing `code_index`, `repo_map_index`, or `recency_tracker`, while teammates inherit the parent `tools` list.
- `code_search` applies `path` filtering after global top-K truncation and compares against raw user path text.
- `code_index` still prunes cache entries without saving when the only change is deletion/shrink.
- `repo_map` currently saves pruned caches, but still caches extractor exceptions as empty file tags.

Fix guide:

- Pass `code_index`, `repo_map_index`, and `recency_tracker` into teammate handler construction, or filter boot-gated tools out of teammate `tools` when backing handlers are not present.
- Normalize scoped paths using `safe_path(...).relative_to(workspace).as_posix()` before calling handlers/indexes. Support `./src` and `src/../src`.
- Apply `code_search` path scope before ranking/truncation. Either add a scoped search root/prefix to `CodeIndex.search()` or collect chunks only under the normalized subtree.
- Thread `[code_search].k` through schema/handler defaults if the config key is meant to affect default top-K.
- Skip the code index cache file itself during chunk collection.
- Persist code-index cache pruning even when there are no pending embeddings.
- Do not cache transient repo-map extractor exceptions as permanent empty results. Cache only known unsupported-language results, or return a distinct unsupported sentinel.
- Include grammar package versions in repo-map extractor identity, not just tree-sitter runtime and query text.
- Ensure `[repo-map]` optional dependencies and `uv.lock` stay in the same commit as feature exposure.

Regression tests:

- Team agents can call every tool visible in their schema list, including boot-gated tools, or those tools are absent.
- `code_search(path="./src")` and `code_search(path="src/../src")` find matches under `src`.
- Scoped search finds a lower-ranked subtree match that would be below global top-K.
- Deleting or shrinking a file removes stale cache entries from disk.
- The cache file is never returned as a semantic-search result.
- A transient extractor exception is retried on the next call.

## Batch 5 - State, Memory, Workflow, Team, And Skill Durability

Source findings include: `9216b78`, `7799a0d`, `1e8e477`, `d7dccc8`, `279e20b`, `e0e3c00`, `fd179ff`, `5d13562`, `7970881`, `bd49e2d`, `140e0f2`, `f79716f`, `291a12b`, `1c4a04b`, `f5aba62`.

Fix guide:

- Memory recall must not inject mid-conversation `role="system"` messages into provider message lists. Fold recall into a provider-safe instruction/user content shape.
- `memory.create()` must refuse existing paths. Intentional replacement should use a replace/update operation.
- Missing OpenAI embedding keys should truly fail open. Do not construct a client with an empty key and then fail during recall.
- `/import` must validate message schema deeply enough to prevent invalid roles/content blocks from poisoning the next provider call.
- `team_register` should validate teammate names against mailbox-compatible rules before persisting.
- `subagent_send` should not append follow-up messages to saved subagent conversation state until the resumed loop succeeds, or it must roll back on failure.
- Pending teammate messages should not be cleared before the main LLM turn succeeds. Preserve them across interruption/LLM errors.
- Workflow node execution should cancel or track running nodes on KeyboardInterrupt; do not leave invisible work alive after rollback.
- Goal evaluator should honor a recorded verdict even if the nested loop later raises after the verdict tool was called.
- Cron/scheduled jobs need overlap policy: skip, queue, replace, or allow one-at-a-time per job key. Do not overlap indefinitely by always using unique run IDs.
- `isolation="worktree"` should fail closed when worktree setup fails, unless user explicitly accepted fallback to main workspace.
- Skill promotion should be durable: move existing skill aside, move draft into place, then clean backup; never delete the live skill before replacement is safely installed.
- Hook config parsing should reject malformed top-level config instead of silently disabling hooks.
- PDF page ranges starting past EOF should return a clear error, not silently map to the last page.
- `semantic_rename` request positions must convert to UTF-16 before sending to the LSP server.
- Workflow `token_budget=0` should mean unlimited if that is the documented override semantics.

Regression tests:

- Provider conversion accepts memory recall without mid-stream system messages.
- Duplicate memory create fails without data loss.
- Team pending messages survive interrupted main turns.
- Workflow interrupt leaves no untracked active workers.
- Goal verdict is read when the verdict tool succeeded even if later loop work fails.
- Worktree isolation failure does not silently run in the main workspace.
- Skill promotion interruption preserves either old live skill or new live skill, never neither.

## Batch 6 - Packaging, Config, And Documentation Corrections

Source findings include: `e09ace3`, `113bc16`, `2a93ece`, `4e291a5`, `8dcfbe8`, `38d7ff0`, `3618287`, `f5118a9`, `c4846c2`, `82c4bcb`, `190/194/195` docs findings in the source report.

Fix guide:

- Resolve provider default API key env through provider presets. The current hard-coded map omits Gemini and other presets.
- Update API key docs to match current behavior: `api_key_env` can be an env-var name or an `sk-` plaintext key, if that remains intentional.
- Keep installed-package config docs separate from repo-development config docs. PyPI users should not be told to edit repository `config.toml`.
- Include repo-layout files required by tests in sdist, or mark those tests as repo-only so packaged test runs do not fail.
- Update README for new env vars such as `BAREAGENT_TEAM_MEMORY_ENABLED`.
- Fix pricing/cache wording: `0.1x` is one-tenth price / 90% off, not "九折".
- Fix docs that name wrong paths, especially `src/memory/session_tree.py` vs `src/bareagent/memory/session_tree.py`.
- Keep common command docs aligned with CI-equivalent commands: prefer `uv run ...` and `bash scripts/ci-check.sh` where CI parity matters.
- Fix tracing docs only after implementation is fixed; docs must not promise privacy controls that code does not enforce.
- Update VitePress/sidebar/command completion docs for new guide chapters and slash commands.

Validation:

- `uv run pytest tests/test_ci_visibility.py`
- docs build if dependencies are installed: `cd docs && npm run docs:build`
- `uv build` or equivalent package metadata check if packaging files changed.

## Batch 7 - Trellis Archive And Journal Hygiene

Source findings: many archive/journal P2/P3 rows in Appendix A of the source report.

This is lower priority than runtime fixes, but it matters for future agents.

Fix guide:

- Do not blindly check every old acceptance checkbox. Only mark an archived criterion complete when there is evidence in commits, CI logs, or session journal summaries.
- Fix broken archived manifest paths caused by moving tasks into `.trellis/tasks/archive/...`. JSONL `file` entries should resolve from repo root.
- For archived tasks with `task.json.status="completed"` but unchecked PRDs, either add an "Archive reconciliation" note with evidence or leave the checkbox unchanged and add a clear reason.
- Replace journal placeholders `(Add details)`, `(see git log)`, and `(Add test results)` where the source commits and validation commands can be reconstructed.
- Add missing commit hashes in journal/index entries when the summary references work not listed in the commit table.
- Consider writing a small validation script for Trellis archives:
  - every JSONL `file` path exists,
  - completed tasks either have checked acceptance criteria or an explicit reconciliation note,
  - no journal placeholders remain in completed sessions,
  - archived task `commit` fields are filled when implementation commits are known.

Suggested handling:

- Keep archive hygiene in a separate PR/commit batch. It is broad and noisy, and should not be mixed with runtime fixes.

## Validation Gate For Every Runtime Batch

Run targeted tests for the changed area first, then the full local gate:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
uv run pytest
bash scripts/ci-check.sh
```

At guide time, `python --version`, `uv run python --version`, and `uv run pytest --version` work in this workspace, so the earlier review-time "missing uv Python" blocker appears resolved locally. Full tests were not run while creating this guide.

## Suggested Commit Order

1. `Fix: align Trellis platform routing for Trae/ZCode/Reasonix`
2. `Fix: harden permission checks for mutating and network tools`
3. `Fix: enforce tracing privacy and session lifecycle`
4. `Fix: merge streamed tool calls and stabilize retry/rendering`
5. `Fix: repair repo_map/code_search scoping and cache persistence`
6. `Fix: harden memory/team/workflow durable state transitions`
7. `Docs: reconcile provider/config/tracing documentation`
8. `Chore(trellis): reconcile archived task and journal metadata`

Split further if any commit would touch unrelated subsystems. Do not mix Batch 7 archive cleanup with runtime fixes.

## Done Criteria

- Current uncommitted Trellis platform changes no longer have unsupported-platform inconsistencies.
- All current-state P1 findings above are fixed or explicitly rejected with rationale in a task/design note.
- Each accepted runtime fix has a regression test.
- `bash scripts/ci-check.sh` passes.
- The source report remains as evidence; this guide is updated if a finding is intentionally deferred.
