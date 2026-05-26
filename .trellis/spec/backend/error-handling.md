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

## Dangerous shell patterns are blocked *before* the handler runs

`PermissionGuard.DANGEROUS_PATTERNS` (regex list in `guard.py`) covers `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`, shell-wrapper bypass (`bash -c`), absolute-path `rm`, `env`-prefix bypass, `curl | sh`, `mkfs`, `dd if=`, `find -delete`, `chmod 777`, etc. Any of these forces a permission prompt regardless of mode (except BYPASS).

**Rule when adding tools that take shell input**: extend `DANGEROUS_PATTERNS` rather than adding ad-hoc checks in the handler. The handler's job is to *execute*; the guard's job is to *gate*. Splitting that boundary would let a future caller invoke the handler directly and skip the check.

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
