# Error Handling

> How BareAgent reports, recovers from, and rejects failures.

The agent runs many fallible operations in a loop (LLM calls, subprocess execution, file I/O, JSON parsing). The handling style is **strict at the boundary, trust on the inside** — and tool failures are *data* the LLM can react to, not crashes that kill the REPL.

---

## Define a custom exception only when a caller needs to catch it specifically

`src/core/loop.py` defines exactly one project-specific exception, and that's because the REPL needs to distinguish "LLM failed, drop the partial user turn" from "Python crashed":

```python
class LLMCallError(Exception):
    """Raised when an LLM call fails or the agent loop exceeds its iteration limit."""
```

Caller in `src/main.py`:

```python
try:
    agent_loop(...)
except LLMCallError:
    del messages[snapshot_len:]
    ui_console.print_error("LLM call failed, please try again.")
```

**Rule**: introduce a new exception class only when a caller has a `try/except` branch that needs to discriminate it from `Exception`. Otherwise raise `ValueError` for bad inputs (`TaskManager._validate_status`, `_validate_agent_name`), `PermissionError` for sandbox violations (`src/core/sandbox.py::safe_path`), `RuntimeError` for unexpected runtime states, or `FileNotFoundError` for missing resources (`TranscriptManager.resume`). The stdlib hierarchy covers ~95% of cases.

---

## Permission is fail-closed for sub-agents and any non-interactive context

`PermissionGuard.ask_user()` in `src/permission/guard.py`:

```python
def ask_user(self, call: Any) -> bool:
    if self.fail_closed:
        return False
    if self.mode == PermissionMode.PLAN:
        print(f"Plan mode: {call.name} blocked (read-only)")
        return False
    if self._ask_user_fn is not None:
        return self._ask_user_fn(call)
    if not sys.stdin.isatty():
        print(f"Non-interactive environment: {call.name} denied")
        return False
    ...
```

Sub-agents inherit a guard with `fail_closed=True` whenever the parent is in PLAN or the child runs in the background:

```python
def for_subagent(self, agent_type, *, background: bool = False) -> PermissionGuard:
    return self.clone(
        mode=resolved_mode,
        fail_closed=self.fail_closed or background or resolved_mode == PermissionMode.PLAN,
    )
```

**Why**: a background or sandboxed agent has no human to ask. Defaulting to "deny" prevents an autonomous agent from approving its own destructive command. **Never** add a code path that approves a tool when `fail_closed=True`.

---

## Safe modes are not overridable by user allow rules

`PermissionMode.PLAN` is a read-only contract the user explicitly opted into. That promise breaks the moment an allow rule can punch through the mode:

```python
# Wrong — allow rule short-circuits before PLAN is checked:
def requires_confirm(self, tool_name, tool_input):
    if self._matches_allow(tool_name, tool_input):
        return False                       # ← user's "trusted" rule lets a write tool through in PLAN
    if self.mode == PermissionMode.PLAN and tool_name not in self.SAFE_TOOLS:
        return self._deny()
```

```python
# Correct — mode-level safety checks come before per-tool allow rules:
def requires_confirm(self, tool_name, tool_input):
    if self.mode == PermissionMode.BYPASS:
        return False                       # explicit escape hatch (named by the user)
    if self.mode == PermissionMode.PLAN and tool_name not in self.SAFE_TOOLS:
        return self._deny()                # PLAN denies regardless of allow rules
    if self._matches_allow(tool_name, tool_input):
        return False
    ...
```

**Rule**: any safety-mode short-circuit (PLAN, plus any future mode whose semantics include "deny by default") must be evaluated *before* allow rules are consulted. Allow rules are a convenience for `DEFAULT` / `AUTO` ergonomics — not a credential the user can present to bypass a mode they explicitly opted into. `BYPASS` is the only intentional escape hatch, and it is opt-in by name.

**Why this is structural, not per-tool**: in PR4 (MCP integration) an allow rule `mcp__github__` matched against `mcp__github__create_issue` would let a write-side MCP tool through even when the user had switched to PLAN. The same hole would re-open for any future tool family whose name happens to match a user's allow prefix. Fixing it once at the mode-check ordering is the only correct solution; per-tool exception lists rot.

---

## Dangerous shell patterns are blocked *before* the handler runs

`PermissionGuard.DANGEROUS_PATTERNS` (regex list in `guard.py`) covers `rm -rf`, forced `git push` / `git clean`, recursive PowerShell `Remove-Item`, `git reset --hard`, `DROP TABLE`, shell-wrapper bypass (`bash -c`), absolute-path `rm`, `env`-prefix bypass, `curl | sh` (including through a transparent prefix such as `curl | sudo sh`, which still feeds stdin to the shell), `mkfs`, `dd if=`, `find -delete`, `chmod 777`, etc. Any of these forces a permission prompt regardless of mode (except BYPASS). Recursive `rm` and world-writable `chmod 777` are also classified from normalized command tokens so option order, long options, quoting, transparent prefixes, and redirections cannot hide them from the legacy regexes.

**Rule when adding tools that take shell input**: extend `DANGEROUS_PATTERNS` rather than adding ad-hoc checks in the handler. The handler's job is to *execute*; the guard's job is to *gate*. Splitting that boundary would let a future caller invoke the handler directly and skip the check.

On Windows, the shell handler executes through PowerShell, whose command names
and parameter names are case-insensitive. Keep the dangerous-pattern set
case-insensitive as a whole, and cover destructive short or bundled flags (for
example `git push -f` and `git clean -fdx`) as well as their long forms. Every
new pattern needs both a destructive regression case and a nearby safe case
such as a dry run or `-WhatIf`, so broader matching does not silently turn AUTO
mode into prompt-on-every-command.

### Git invocations are classified from normalized tokens

A literal `git <subcommand>` regular expression is not a sufficient boundary:
the same executable can be invoked as `git.exe`, through a quoted absolute
path, or with Git global options (`-C`, `-c`, `--no-pager`, and related forms)
before the subcommand. PowerShell also uses `& "...\git.exe"` for quoted
executable paths. All of these forms reach the shell handler unchanged.

Use the shared Git invocation parser in `permission/guard.py` for both
dangerous and DEFAULT-mode read-only classification. It must:

- tokenize without executing, strip shell quotes, and compare the final path
  component case-insensitively against `git` / `git.exe`;
- skip value-taking and flag-style Git global options before locating the
  subcommand;
- force confirmation for forced/mirror/pruned push, hard reset, and branch deletion
  before consulting allow rules;
- treat `git push --delete` / `-d`, delete refspecs (`:main`, `:refs/heads/x`),
  and force refspecs (`+main`, `+src:dst`) as destructive push forms even when
  they appear after `--` or without an explicit `--force` flag;
- keep option boundaries precise so `clean -n`, ordinary refspecs such as
  `main:main`, and a ref such as `release-f` remain safe neighbors;
- recognize Git's unique long-option abbreviations (`--mir`, `--pru`,
  `--dele`, `--forc`, `--har`, and `--out`) while excluding `--no-*` forms;
- treat tokenization failure as not newly safe while retaining the legacy
  dangerous regexes as a conservative fallback.

Regression tests in `tests/test_dangerous_patterns.py` must exercise `bash`
and `background_run`, direct `is_dangerous` results, allow-rule ordering,
fail-closed denial, quoted/full-path executables, global options, and DEFAULT
read-only variants. Never execute a destructive Git command to test the guard.

### Recursive rm and world-writable chmod are classified from tokens

A literal `rm -[flags]r` or `chmod 777` regular expression is not enough:
option order (`rm -f -r`), long options (`--recursive`, `--force`), leading
zeros in modes (`0777`), quoted arguments (`rm '-rf'`), quote-concatenated
executables (`r''m`), transparent prefixes (`command` / `sudo`), and
redirections can all hide the same destructive intent from the legacy regexes.

Use the shared shell token views in `permission/guard.py` to classify these
forms before allow rules:

- treat an `rm` / `rm.exe` executable or the Windows PowerShell `Remove-Item`
  cmdlet and its aliases (`del`, `erase`, `rd`, `rmdir`, `ri`) as destructive
  when any argument before `--` is recursive (`-r` / bundled short flags /
  `--recursive`, including PowerShell `-Recurse:$true`); a `-WhatIf` or
  `-WhatIf:$true` dry run remains safe, while `-WhatIf:$false` does not;
- treat a `chmod` / `chmod.exe` executable as destructive when any argument is
  a numeric mode granting world rwx (`777`, `0777`, `00777`, `1777`, `4777`,
  `7777`, …), including when combined with `-R`;
- treat GNU `rm`'s unique `--recursive` abbreviations (`--r`, `--re`,
  `--rec`, `--recursi`, `--recursiv`, and longer prefixes) as recursive too;
- resolve sudo's value-taking and flag long options by their unique prefixes
  (`--us` / `--u` -> `--user`, `--chd` -> `--chdir`, `--non` ->
  `--non-interactive`, and `--preserve-e` -> `--preserve-env`) so an
  abbreviated sudo option cannot hide the destructive command;
- honor command position via transparent prefixes and strip redirections the
  same way Git/wrapper detection does;
- keep non-recursive `rm -f file`, `rm -- -rf`, and non-777 modes such as
  `chmod 755` / `chmod +x` as safe neighbors.

Regression tests in `tests/test_dangerous_patterns.py` must cover option-order
and long-option rm forms, quoted/ANSI-C/quote-concatenated variants, absolute
paths, transparent prefixes, redirections, chmod leading-zero modes, allow-rule
ordering, and nearby safe neighbors. Never execute a destructive command to
test the guard.

### Automatic shell safety applies to the complete simple command

DEFAULT-mode automatic approval is a whole-command decision. A read-only Git
invocation or an `AUTO_SAFE_PATTERNS` prefix is safe only when the complete
input is one well-formed simple command. Unquoted separators, CR/LF,
redirections, grouping, active `$()` / backtick substitution, and malformed
quoting must remove that shortcut; a safe first command does not authorize the
remaining shell text. Quoted control characters remain ordinary argument
content.

Keep the read-only grant view separate from dangerous discovery. AUTO retains
its allow-by-default behavior for otherwise unknown compound commands, while
known destructive commands and opaque launchers are classified before allow
rules. Inspect active substitution bodies recursively for the same known
dangerous Git, wrapper, and regex patterns, but do not label a benign
substitution dangerous merely because it is executable syntax.

Shell token boundaries and command-prefix trimming must use only ASCII space
and tab. Do not use Unicode-aware `strip()`, `isspace()`, or `\s` to grant an
automatic shell shortcut: VT, FF, CR/LF, and Unicode separators are not parsed
consistently by PowerShell and POSIX shells and therefore must fail closed.

### Opaque launcher detection follows the real command position

Treat shell payload launchers as dangerous when they occupy an executable
command position: `sh`-family `-c`, PowerShell/pwsh command or encoded-command
forms, `cmd /c` or `/k`, `env` with arguments, and PowerShell's `&` call
operator when its target is an expression, script block, or variable (for
example, `& (Get-Alias ri) -Recurse build`), plus PowerShell's
`Invoke-Expression` / `iex` evaluators. Normalize quoted/full-path
executables, shell-specific quote concatenation, escapes, and line
continuations without executing the input; never evaluate a dynamic call
target to decide whether it is safe.

The command-position parser must pass through only explicitly modeled prefixes
and their real option grammar: assignments, redirections, `sudo`, and the
`command`, `exec`, and `nohup` launch prefixes. Non-executing forms such as
`command -v` / `-V` and `nohup --help` / `--version` terminate this traversal.
PowerShell script-block braces (`{` / `}`) are command boundaries too: inspect
commands inside `ForEach-Object`, `if`, and similar blocks (including aliases
such as `ri`) without executing the script. Preserve escaped literal braces as
arguments rather than treating them as script blocks.
For redirections embedded in an argv segment, remove both the operator and its
target before parsing the remaining Git or wrapper tokens; never reinterpret a
redirection target as a command argument.

Every parser expansion needs positive destructive/opaque cases and close
non-executing neighbors. Classification tests operate on strings only and must
never invoke the represented shell command.

---

## Scenario: Automatic Git worktree cleanup

### 1. Scope / Trigger

This contract applies when a sub-agent worktree is finalized automatically.
Cleanup can destroy both uncommitted files and commits, so uncertainty must
retain the worktree or branch rather than guessing that it is disposable.

### 2. Signatures

- `WorktreeHandle.base_commit: str | None` records the exact creation point.
- `worktree_status(handle: WorktreeHandle) -> tuple[bool, str]` reports whether
  any work or inspection uncertainty requires retention.
- `remove_pristine_worktree(handle) -> tuple[bool, bool, str]` separately
  reports worktree removal, branch removal, and a human-readable reason.

### 3. Contracts

- `git worktree add` is pinned to the recorded `base_commit`.
- Automatic cleanup runs only after successful empty porcelain status and a
  successful HEAD lookup equal to `base_commit`.
- Worktree removal is non-forced so Git can reject changes that appear after
  the status check.
- Branch deletion uses `git update-ref -d <ref> <base_commit>` so deletion is
  atomic and succeeds only while the ref still has the expected value.
- Forced removal remains an explicit/manual cleanup operation, never the
  automatic finalizer path.

### 4. Validation & Error Matrix

- Status exception or non-zero exit -> keep worktree and branch.
- Non-empty porcelain status -> keep worktree and branch.
- Missing creation commit -> keep worktree and branch.
- HEAD exception, non-zero exit, or mismatch -> keep worktree and branch.
- Non-forced worktree removal refusal -> keep worktree and branch.
- Worktree removed but expected-ref deletion fails -> keep the branch and
  report that only the worktree path was removed.
- Every check succeeds and the ref is unchanged -> remove both.

### 5. Good / Base / Bad Cases

- Good: a pristine unchanged worktree is removed automatically.
- Base: a dirty worktree or clean branch with a new commit is retained and its
  location is reported.
- Bad: unavailable Git status is treated as clean, followed by `--force` and
  `branch -D`; this can erase work precisely when inspection failed.

### 6. Tests Required

`tests/test_worktree.py` must assert clean cleanup, uncommitted retention,
committed-ahead retention, status and HEAD exception/non-zero handling, and
the two post-check races: a late file makes non-forced removal fail, while a
late commit makes expected-ref deletion preserve the branch.

### 7. Wrong vs Correct

```python
# Wrong: check-then-force-delete has a race and discards unknown state.
if not dirty:
    git("worktree", "remove", "--force", path)
    git("branch", "-D", branch)

# Correct: Git re-checks the worktree and atomically guards the branch ref.
git("worktree", "remove", path)
git("update-ref", "-d", f"refs/heads/{branch}", base_commit)
```

---

## Tool handlers return errors as structured output, not exceptions

A handler that raises propagates to `agent_loop` and crashes the iteration. Instead, handlers report failures as text so the LLM can read them and decide what to do.

Example — `src/core/handlers/bash.py`:

```python
except subprocess.TimeoutExpired as exc:
    output = _join_output(exc.stdout, exc.stderr)
    if output:
        message = f"Error: command timed out after {timeout} seconds\n{output}"
    else:
        message = f"Error: command timed out after {timeout} seconds"
    if raise_on_error:
        raise RuntimeError(message) from exc
    return message
```

And the loop's safety net for any escaped exception (`src/core/loop.py`):

```python
try:
    output = handler(**call.input)
except Exception as exc:
    output = f"Error: {type(exc).__name__}: {exc}"
    results.append(_tool_result(call.id, output, is_error=True))
    continue
```

**Rule**: tool handlers must catch their own predictable failures (timeout, missing file, bad JSON) and return a human-readable string starting with `Error:`. The loop's blanket `except Exception` is the safety net, not the primary mechanism. The `_tool_result(..., is_error=True)` flag lets the LLM see this was an error without confusing it with normal output.

Hard validation failures (programmer error, not user/LLM error) still raise — e.g. `ValueError("offset must be >= 0")` in `src/core/handlers/file_read.py`. Those are bugs, not data.

---

## Multimodal handlers: success returns `list[dict]`, errors return `str`

`_tool_result` in `src/core/loop.py` accepts both shapes:

```python
def _tool_result(
    tool_use_id: str,
    output: str | list[dict[str, Any]],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    if isinstance(output, list):
        content: Any = output                    # list of content blocks, passed through
    else:
        content = stringify(output)              # legacy text path
    ...
```

A handler that wants to emit multimodal output (image, etc.) returns a `list[dict]` of provider-neutral content blocks on the **success** path. Any error — unhealthy server, JSON-RPC error, `isError: true`, missing argument — still returns a `str` starting with `Error:`. The two shapes are not interchangeable:

```python
# Correct — multimodal handler in src/mcp/registry.py
def _make_handler(...):
    def handler(**kwargs) -> str | list[dict[str, Any]]:
        try:
            result = client.call_tool(tool_name, kwargs)
        except MCPCallError as exc:
            return str(exc)                       # error → string
        if result.get("isError"):
            return f"Error: {_flatten_content(result.get('content', []))}"  # isError → string
        return _to_content_blocks(result.get("content", []))   # success → list[dict]
    return handler
```

**Why the split**: providers serialize the two cases differently. Anthropic puts a `list[dict]` straight into `tool_result.content`; OpenAI lifts image blocks out into a follow-up `user` message (`role: "tool"` cannot carry `image_url`). Errors don't need that machinery — they're plain text the LLM reads and reacts to. Returning `list[dict]` for an error would force every provider's error path through the multimodal lift logic for no reason, and it would also defeat the `_tool_result(..., is_error=True)` flag that downstream consumers use to filter error noise.

**When adding a new multimodal handler** (audio, embedded resource passthrough, image generation, etc.):
- Success path: return `list[dict]`; normalize foreign formats (e.g. MCP image → Anthropic-native shape) at the registry boundary, not in the provider.
- Error path: catch the predictable failures and return `Error: <message>` string. Let the loop's blanket `except Exception` cover the unpredictable ones.
- Do not mix shapes inside one path (no half-text half-list returns).

---

## Provider failures surface as `LLMCallError` with the original cause attached

`src/core/loop.py`:

```python
except BaseException as exc:
    llm_span.set_error(str(exc) or type(exc).__name__)
    _safe_log_response(..., error=str(exc) or type(exc).__name__)
    if not isinstance(exc, Exception):
        raise
    msg = f"LLM call failed: {type(exc).__name__}: {exc}"
    if console is not None:
        console.print_error(msg)
    raise LLMCallError(msg) from exc
```

Two important details:

- `BaseException` is caught so `KeyboardInterrupt` and `SystemExit` are *not* swallowed — they re-raise on the `if not isinstance(exc, Exception): raise` line.
- `raise LLMCallError(msg) from exc` keeps the original traceback in `__cause__` so `/log` and tracing backends still see what the SDK actually raised.

**Rule**: when wrapping an exception, always use `raise NewError(...) from exc`. Never bare `raise NewError(...)` after catching — it loses the chain.

Streaming has its own fallback path: if `create_stream()` raises `NotImplementedError` before any event arrives, `_fallback_to_non_stream()` retries with `create()`. This is a deliberate exception used as a control-flow signal, not an error.

---

## Validate at the boundary; trust internal callers

Boundaries that validate input:

- **User input**: `_validate_mode` in `src/main.py`, `_validate_agent_name` in `src/team/mailbox.py`, `_validate_session_id` in `src/debug/interaction_log.py`.
- **External APIs**: provider response parsing (`OpenAIProvider._parse_response`, etc.) normalizes wire-format quirks into the typed `LLMResponse` dataclass.
- **File-system paths**: `src/core/sandbox.py::safe_path` rejects absolute paths, `~` expansion, symlinked components, and parent-directory escapes.

Once data has crossed a boundary, internal callers trust the types. **Do not** scatter `if not isinstance(x, str):` checks deep inside `TaskManager` — the dataclass and the boundary validation already guarantee it. Defensive coding noise hides real bugs.

---

## Scenario: LSP WorkspaceEdit target confinement

### 1. Scope / Trigger

This contract applies whenever a language-server response can write files,
currently semantic rename via LSP `WorkspaceEdit`. Server-returned URIs are
external input even when the initiating file was already permission-approved.

### 2. Signatures

- `apply_workspace_edit(workspace_edit, *, workspace_root: str | Path)` requires
  the security boundary explicitly.
- The production semantic-rename handler passes
  `LanguageServerManager.repository_root`; callers must not infer a root from
  the process working directory.

### 3. Contracts

- Parse and require URI scheme `file` before converting the URI to a path.
- Derive a relative path from `workspace_root`, then call
  `safe_path(relative, Path(workspace_root))` before any read or write.
- The shared sandbox check owns canonicalization, parent traversal, cross-drive,
  and symlink rejection.
- Rejected, malformed, unsupported, or unreadable targets are appended to
  `WorkspaceEditResult.skipped`; other safe file groups continue to apply.
- Result summaries label these generally as skipped WorkspaceEdit entries, not
  only as resource operations.

### 4. Validation & Error Matrix

- Missing or non-`file` scheme -> skip as unsupported.
- Malformed URI parse -> skip as invalid and continue.
- Cross-drive `relpath` failure -> skip as unsafe.
- Parent escape or resolved target outside root -> skip as unsafe.
- Symlink anywhere in the candidate chain -> skip as unsafe.
- `Path.resolve` symlink loop (`RuntimeError`) -> skip as unsafe.
- In-root path that cannot be read -> skip as unreadable.
- Valid in-root path -> apply edits bottom-up and atomically write it.

### 5. Good / Base / Bad Cases

- Good: a multi-file rename updates every target inside the repository.
- Base: a mixed response updates safe files and reports outside targets without
  touching them.
- Bad: converting a server URI directly to an absolute path and opening it;
  permission for the initiating file does not authorize arbitrary response
  targets.

### 6. Tests Required

`tests/test_lsp_workspace_edit.py` must cover both WorkspaceEdit shapes,
outside-root targets, symlink escape, opaque non-file URIs, malformed URI
continuation, and normal in-root application. `tests/test_lsp_tools.py` must
exercise manager-root propagation with mixed safe/unsafe entries in both
`changes` and `documentChanges` forms.

### 7. Wrong vs Correct

```python
# Wrong: the language server controls an unrestricted absolute write target.
path = document_uri_to_path(uri)
atomic_write_text(Path(path), updated)

# Correct: the manager supplies the root and the shared sandbox resolves it.
relative = os.path.relpath(document_uri_to_path(uri), start=workspace_root)
path = safe_path(relative, Path(workspace_root))
atomic_write_text(path, updated)
```

---

## Background failures must not leak silently

`BackgroundManager._run` in `src/concurrency/background.py`:

```python
def _run(self, task_id: str, fn, *args) -> None:
    try:
        result = fn(*args)
    except Exception as exc:
        self._queue.put({"task_id": task_id, "status": "failed",
                         "error": f"{type(exc).__name__}: {exc}"})
        return
    self._queue.put({"task_id": task_id, "status": "done", "result": result})
```

A background thread that crashes posts a `failed` notification with the error message. `AutonomousAgent.run` does similar: `logging.exception("Task %s failed in agent %s", task.id, self.name)` then continues the loop (one teammate dying must not kill the daemon).

**Rule**: every long-running background entry point (threads, daemonized agents) must catch `Exception` at its top frame and report the failure to a queue or log. Letting a daemon thread die silently is a debugging trap.

---

## Long-lived connection readers must distinguish graceful close from unexpected disconnect

Any background reader watching an external endpoint that can either be torn down by us (`close()` called) or die on its own (subprocess EOF, SSE stream broken, socket reset) needs a way to tell the two apart. The pattern `src/mcp/transport/` follows:

- A `_closing` boolean is set by `close()` *before* the underlying stream is dismantled.
- The reader thread's `finally:` block inspects `_closing` and only fires `_invoke_disconnect(reason)` when it is `False`.
- `_invoke_disconnect` is idempotent (`_disconnect_invoked` latch) — if the reader detects a half-closed stream and then the `finally` also tries to fire, the user only sees one notification.
- The disconnect handler (`Transport.set_disconnect_handler`) is registered by the orchestrator (`MCPManager._build_client`) and routes to `manager._on_disconnect(name, reason)`, which marks the server `UNHEALTHY` and pushes a `BackgroundManager.notify` event so the REPL surfaces the failure before the next LLM turn.

```python
# In the reader thread's finally:
if not self._closing:
    self._invoke_disconnect(reason or "stream ended unexpectedly")
self._fail_all_pending("…")
```

**Why this matters**: without the `_closing` flag, a clean `close_all()` at shutdown would race the reader's EOF detection and fire a spurious "MCP server X disconnected" toast every time the user hit `/exit`. With it, the user only sees disconnect notifications for *real* failures — which is the whole point of the proactive channel.

**Rule**: any new transport / long-lived reader (LSP client, future websocket bridge, etc.) must replicate this three-piece structure: a `_closing` flag set on user-initiated teardown, an `_invoke_disconnect`-style one-shot hook fired only when `_closing` is false, and an orchestrator-level handler that marks the resource unhealthy + notifies the user through the same channel as other async events.
