# Logging & Output

> Where text goes when BareAgent has something to say.

**`logging.getLogger(__name__)` is not the project's default output mechanism.** Reaching for the stdlib `logging` module is almost always wrong (the codebase has ~5 deliberate exceptions; see the bottom of this file). BareAgent has four distinct output channels, each with a specific purpose. Pick the right one — they are not interchangeable.

---

## The four channels

| Channel | Purpose | API | Persists? |
|---|---|---|---|
| User-facing console | Status messages, errors, tool call/result panels, prompts | `AgentConsole` (`src/ui/console.py`) | No (terminal only) |
| Streaming LLM output | Live tokens as the model generates them | `StreamPrinter` (`src/ui/stream.py`) | No |
| Structured interaction log | Full LLM request + response payloads, one file per round-trip | `InteractionLogger` (`src/debug/interaction_log.py`) | Yes — `.logs/<session>/` |
| Distributed tracing | Spans, tags, timings for `llm_call`, `tool_execution`, etc. | `tracer` proxy (`src/tracing/`) | Yes — depends on backend |

**`print()` is forbidden in runtime code paths.** The only legitimate uses are:

- `src/main.py` config-bootstrap errors (lines ~1474, 1477, 1483), where the console doesn't yet exist because config parsing failed before it could be built.
- `src/permission/guard.py::ask_user` (~3 calls), as the fallback prompt when no `_ask_user_fn` was injected. Any caller that has a console must inject a callable that uses `AgentConsole` instead.

All other text-to-terminal output goes through `AgentConsole`.

---

## User-facing output: `AgentConsole`

`src/ui/console.py` wraps `rich.console.Console` with themed panels for tool calls, results, status, and errors:

```python
ui_console.print_status("Permission mode: default")
ui_console.print_error("LLM call failed, please try again.")
ui_console.print_tool_call(name, input_data)   # rounded panel, JSON syntax-highlighted
ui_console.print_tool_result(name, output)     # truncated to 2000 chars
ui_console.print_assistant(text)               # markdown-rendered
```

**Why**: every visible string benefits from theming (catppuccin-mocha, dracula, nord, …) and consistent panel framing. Using `print()` bypasses the theme and breaks layout under `prompt_toolkit`'s alternate screen.

**Rule for new code**: accept a `UIProtocol` (`src/ui/protocol.py`) in any function that might emit user-visible text. The agent loop, tool handlers, and managers all receive `console: UIProtocol | None` and check for `None` before calling — this keeps them testable without a real terminal. See `agent_loop()` in `src/core/loop.py` for the pattern.

---

## Streaming model output: `StreamPrinter`

When `provider.create_stream()` is in use, the loop pulls tokens off the generator and feeds them to `StreamPrinter` so the user sees output incrementally. Don't print stream text yourself — let the printer handle it.

`AgentConsole.get_stream_printer()` returns a printer bound to the same `Console`, so themes apply. `_consume_stream` in `src/core/loop.py` is the only call site you need to understand.

---

## Structured interaction logs: `InteractionLogger`

`InteractionLogger` captures the complete LLM payload (messages, tools, response text, thinking, tool calls, token counts, duration, errors) for every round-trip. Files are sequence-numbered under `.logs/<session_id>/`:

```python
log_seq = interaction_logger.log_request(messages, tools, provider_info=...)
# ... LLM call happens ...
interaction_logger.log_response(
    log_seq,
    text=response.text,
    thinking=response.thinking,
    tool_calls=_serialize_tool_calls(response.tool_calls),
    input_tokens=response.input_tokens,
    output_tokens=response.output_tokens,
    duration_ms=...,
)
```

**When to use**: only `agent_loop` calls this directly; it is a fixed capture point for the LLM boundary. **Do not** add `interaction_logger.log_*` calls inside handlers or providers. If you need deeper inspection, add a tracing span instead (see below).

The captured payload powers `/log` REPL commands and the `web_viewer.py` SPA. Enabling requires `[debug] enabled = true` in `config.toml` or `BAREAGENT_DEBUG=1`. Failures inside the logger are swallowed and reported via `console.print_status` (see `_safe_log_request` / `_safe_log_response`) — logging must never crash the agent.

---

## Tracing spans: `tracer.trace(...)`

`src/tracing/` defines a `Tracer` ABC and a global proxy. Backends include `NullTracer` (default no-op), `JsonFileTracer` (always-on JSONL), `LangfuseTracer`, and `OpenTelemetryTracer`. Use it for cross-call observability:

```python
from src.tracing import tracer as global_tracer

with global_tracer.trace("llm_call", tags={"model": model_name}) as llm_span:
    try:
        response = provider.create(...)
    except BaseException as exc:
        llm_span.set_error(str(exc) or type(exc).__name__)
        raise
    llm_span.set_tag("input_tokens", response.input_tokens)
    llm_span.set_tag("output_tokens", response.output_tokens)
    llm_span.set_content_tag("output", response.text)
```

**When to use**: any operation worth measuring across runs (latency, error rates, token costs) or worth attaching to a distributed trace. Existing spans: `llm_call`, `tool_execution`. Add new spans at coarse-grained boundaries — not inside tight loops.

`set_tag` is for metadata (model name, tool name, counts). `set_content_tag` is for potentially-large content (messages, outputs); backends may drop these when `[tracing] content_enabled = false`.

---

## When `logging.*` is acceptable

There are two narrow patterns where stdlib `logging` appears in the codebase. Together they cover ~5 call sites total (`grep -rn "logging\." src/`); the list should not grow without a strong reason.

1. **Library-style warnings about misconfiguration or graceful fallbacks** — for code paths that run far from a console or shouldn't break the user's flow:
   - `src/provider/factory.py`: `logging.warning("Invalid thinking mode %r, falling back to 'adaptive'", mode)` at startup.
   - `src/planning/agent_types.py`: `_log.warning("Unknown agent type %r, falling back to %r", …)` in `resolve_agent_type`.
   - `src/memory/compact.py`: `logger.warning("Context compression failed", exc_info=True)` when the LLM summarization call fails and the loop must roll back.
2. **Daemon-thread tracebacks** — for threads that have no console to write to:
   - `src/team/autonomous.py`: `logging.exception("Task %s failed in agent %s", …)` inside the autonomous loop.

**Rule**: do not add `logger = logging.getLogger(__name__)` to new modules. If you need to surface something:

- A user should act on it → `AgentConsole.print_error` / `print_status`.
- It's debug-time-only inspection of LLM behavior → already captured by `InteractionLogger`.
- It's a measurement → `tracer.trace(...)`.
- It's a library-style fallback warning in a deeply-nested utility, or a daemon-thread error with no console available → `logging.warning` / `logging.exception` is acceptable as a last resort. Match the existing style (module-level `_log = logging.getLogger(__name__)` or bare `logging.warning(...)`).

---

## What never gets logged

- **API keys**: `factory.py` reads them from environment variables; the keys never appear in messages, tracing tags, or transcripts. Do not log `os.environ` snapshots or full provider clients.
- **User config file contents**: `config.local.toml` may contain secrets and is git-ignored. Never echo it back to the console or persist it.
- **`tool_input` blobs containing pasted credentials**: `print_tool_call` truncates to 2000 chars and the permission preview to 500 chars (`MAX_PERMISSION_PREVIEW_CHARS`). When adding new console renderings, preserve these caps.

If you have to add a new sensitive-input tool, document the redaction strategy in its handler docstring — don't hope a downstream sink will scrub it.
