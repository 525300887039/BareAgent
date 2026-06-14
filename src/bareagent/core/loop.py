from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any, cast

from bareagent.concurrency.notification import inject_notifications
from bareagent.core.fileutil import stringify
from bareagent.core.retry import RetryPolicy, run_with_retry
from bareagent.provider.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall
from bareagent.tracing import tracer as global_tracer
from bareagent.ui.protocol import StreamProtocol, UIProtocol
from bareagent.ui.stream import StreamPrinter

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    """Raised when an LLM call fails or the agent loop exceeds its iteration limit."""


class _StreamingUnavailableError(RuntimeError):
    """Raised when streaming is explicitly unsupported before any events arrive."""


def agent_loop(
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    handlers: dict[str, Callable[..., Any]],
    permission: Any = None,
    compact_fn: Callable[[list[dict[str, Any]]], None] | None = None,
    bg_manager: Any = None,
    stream: bool = False,
    console: UIProtocol | None = None,
    max_iterations: int = 200,
    interaction_logger: Any = None,
    token_tracker: Any = None,
    hook_engine: Any = None,
    retry_policy: RetryPolicy | None = None,
    skill_gen: Any = None,
) -> str:
    compact = compact_fn or (lambda _messages: None)
    hook_session_id = _resolve_hook_session_id(compact_fn)
    hook_cwd = os.getcwd()
    # Tool calls made during THIS user turn (accumulated across iterations).
    # Fed to ``skill_gen`` only on normal completion so a failed/aborted turn
    # never counts toward experiential skill generation. Sub-agents pass
    # skill_gen=None, keeping generation a main-loop-only concern (like hooks).
    turn_tool_calls = 0

    for _iteration in range(max_iterations):
        _run_background(bg_manager, messages)
        compact(messages)

        log_seq, log_started_at = _safe_log_request(
            interaction_logger=interaction_logger,
            messages=messages,
            tools=tools,
            provider=provider,
            console=console,
        )

        model_name = getattr(provider, "model", "unknown")
        with global_tracer.trace("llm_call", tags={"model": model_name}) as llm_span:
            try:
                response, streamed_output, displayed_tool_calls = _invoke_provider(
                    provider=provider,
                    messages=messages,
                    tools=tools,
                    stream=stream,
                    console=console,
                    retry_policy=retry_policy,
                )
            except BaseException as exc:
                llm_span.set_error(str(exc) or type(exc).__name__)
                _safe_log_response(
                    interaction_logger=interaction_logger,
                    log_seq=log_seq,
                    console=console,
                    duration_ms=(time.monotonic() - log_started_at) * 1000,
                    error=str(exc) or type(exc).__name__,
                )
                if not isinstance(exc, Exception):
                    raise
                msg = f"LLM call failed: {type(exc).__name__}: {exc}"
                if console is not None:
                    console.print_error(msg)
                raise LLMCallError(msg) from exc

            llm_span.set_tag("input_tokens", response.input_tokens)
            llm_span.set_tag("output_tokens", response.output_tokens)
            llm_span.set_content_tag("output", response.text)

        # Aggregate token usage here so both streaming and non-streaming paths
        # (both return through _invoke_provider) are covered at a single point.
        if token_tracker is not None:
            token_tracker.record(response, model_name)

        _safe_log_response(
            interaction_logger=interaction_logger,
            log_seq=log_seq,
            console=console,
            text=response.text,
            thinking=response.thinking,
            tool_calls=_serialize_tool_calls(response.tool_calls),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration_ms=(time.monotonic() - log_started_at) * 1000,
        )

        messages.append(response.to_message())
        if response.text and console is not None and not streamed_output:
            console.print_assistant(response.text)
        if not response.has_tool_calls:
            if not response.text:
                _warn_empty_response(response, console)
            if skill_gen is not None:
                skill_gen.note_turn(turn_tool_calls)
            return response.text or ""

        turn_tool_calls += len(response.tool_calls)
        results: list[dict[str, Any]] = []
        for call in response.tool_calls:
            if console is not None and call.id not in displayed_tool_calls:
                console.print_tool_call(call.name, call.input)

            if _requires_confirmation(permission, call):
                if not _ask_permission(permission, call):
                    denied = "User denied."
                    if console is not None:
                        console.print_tool_result(call.name, denied)
                    results.append(_tool_result(call.id, denied, is_error=True))
                    continue

            if hook_engine is not None:
                outcome = hook_engine.run_pre_tool_use(
                    call.name,
                    call.input,
                    session_id=hook_session_id,
                    cwd=hook_cwd,
                )
                if outcome.block:
                    blocked = outcome.reason or "Blocked by PreToolUse hook."
                    if console is not None:
                        console.print_tool_result(call.name, blocked)
                    results.append(_tool_result(call.id, blocked, is_error=True))
                    continue

            handler = handlers.get(call.name)
            if handler is None:
                output = f"Unknown tool: {call.name}"
                if console is not None:
                    console.print_tool_result(call.name, output)
                results.append(_tool_result(call.id, output, is_error=True))
                continue

            try:
                with global_tracer.trace("tool_execution", tags={"tool": call.name}) as tool_span:
                    tool_span.set_content_tag("input", call.input)
                    output = handler(**call.input)
                    tool_span.set_content_tag("output", stringify(output))
            except Exception as exc:
                output = f"Error: {type(exc).__name__}: {exc}"
                if console is not None:
                    console.print_tool_result(call.name, output)
                results.append(_tool_result(call.id, output, is_error=True))
                continue

            if hook_engine is not None:
                hook_engine.run_post_tool_use(
                    call.name,
                    call.input,
                    output,
                    is_error=False,
                    session_id=hook_session_id,
                    cwd=hook_cwd,
                )

            if console is not None:
                console.print_tool_result(call.name, output)
            results.append(_tool_result(call.id, output))

        messages.append({"role": "user", "content": results})

    msg = f"Agent loop exceeded {max_iterations} iterations"
    if console is not None:
        console.print_error(msg)
    raise LLMCallError(msg)


def _warn_empty_response(response: LLMResponse, console: UIProtocol | None) -> None:
    """Surface a non-fatal diagnostic for a degenerate empty response.

    Fires when the model stops normally yet produced neither text nor tool
    calls -- usually a wire_api/model mismatch, a relay returning an empty
    output array, or the model declining to answer. This does not change the
    loop's control flow: it still returns "" as before. Always logged (so
    console-less sub-agents/teammates leave a trace); also shown on the console
    when one is attached.
    """
    message = (
        "LLM returned an empty response (no text or tool calls) -- "
        f"stop_reason={response.stop_reason!r}, output_tokens={response.output_tokens}. "
        "Possible wire_api/model mismatch or relay issue."
    )
    logger.warning(message)
    if console is not None:
        console.print_status(message)


def _invoke_provider(
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    stream: bool,
    console: UIProtocol | None,
    retry_policy: RetryPolicy | None = None,
) -> tuple[LLMResponse, bool, set[str]]:
    # The whole provider call (including stream consumption, D5) is wrapped in
    # run_with_retry so retryable transient failures (429 / 5xx / connection
    # timeouts) back off and retry. When retry_policy is None / disabled the
    # behavior is identical to a single direct call (backward compatible).
    # _StreamingUnavailableError / NotImplementedError are control-flow signals
    # with no status_code and class names outside the retryable set, so
    # is_retryable returns False for them — the streaming fallback is unaffected.
    def _call() -> tuple[LLMResponse, bool, set[str]]:
        return _invoke_provider_once(
            provider=provider,
            messages=messages,
            tools=tools,
            stream=stream,
            console=console,
        )

    if retry_policy is None:
        return _call()

    return run_with_retry(
        _call,
        retry_policy,
        on_retry=_make_retry_notifier(console, retry_policy),
    )


def _make_retry_notifier(
    console: UIProtocol | None,
    policy: RetryPolicy,
) -> Callable[[BaseException, int, float], None] | None:
    if console is None:
        return None

    def _notify(exc: BaseException, next_attempt: int, delay: float) -> None:
        console.print_status(
            f"LLM call failed ({type(exc).__name__}), retrying in {delay:.1f}s "
            f"(attempt {next_attempt}/{policy.max_attempts})..."
        )

    return _notify


def _invoke_provider_once(
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    stream: bool,
    console: UIProtocol | None,
) -> tuple[LLMResponse, bool, set[str]]:
    if not stream:
        return provider.create(messages=messages, tools=tools), False, set()

    try:
        stream_iter = provider.create_stream(messages=messages, tools=tools)
    except Exception as exc:
        if not _is_streaming_unsupported(exc):
            raise
        return _fallback_to_non_stream(
            provider=provider,
            messages=messages,
            tools=tools,
            console=console,
            exc=exc,
        )

    try:
        return _consume_stream(stream_iter, console=console)
    except _StreamingUnavailableError as exc:
        cause = exc.__cause__ or exc
        return _fallback_to_non_stream(
            provider=provider,
            messages=messages,
            tools=tools,
            console=console,
            exc=cause,
        )


def _fallback_to_non_stream(
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    console: UIProtocol | None,
    exc: BaseException,
) -> tuple[LLMResponse, bool, set[str]]:
    if console is not None:
        console.print_status(
            f"Streaming unavailable, falling back to non-stream mode ({type(exc).__name__})."
        )
    return provider.create(messages=messages, tools=tools), False, set()


def _is_streaming_unsupported(exc: Exception) -> bool:
    return isinstance(exc, NotImplementedError)


def _consume_stream(
    stream_iter: Any,
    *,
    console: UIProtocol | None,
) -> tuple[LLMResponse, bool, set[str]]:
    printer = _get_stream_printer(console)
    displayed_tool_calls: set[str] = set()
    saw_stream_event = False
    streamed_any_text = False
    printer.start()

    try:
        while True:
            try:
                event = next(stream_iter)
            except StopIteration as stop:
                streamed_text = printer.finish()
                response = stop.value
                if response is None:
                    raise RuntimeError("Streaming provider did not return a response.") from None
                return (
                    response,
                    streamed_any_text or bool(streamed_text),
                    displayed_tool_calls,
                )

            saw_stream_event = True
            if event.type == "text" and bool(event.text):
                streamed_any_text = True
            _handle_stream_event(
                event=event,
                printer=printer,
                console=console,
                displayed_tool_calls=displayed_tool_calls,
            )
    except Exception as exc:
        printer.finish()
        if not saw_stream_event and _is_streaming_unsupported(exc):
            raise _StreamingUnavailableError() from exc
        raise


def _handle_stream_event(
    event: StreamEvent,
    *,
    printer: StreamProtocol,
    console: UIProtocol | None,
    displayed_tool_calls: set[str],
) -> None:
    if event.type == "text":
        printer.feed(event.text)
        return

    if event.type != "tool_call":
        return

    printer.finish()
    if event.tool_call_id:
        displayed_tool_calls.add(event.tool_call_id)
    if console is not None:
        console.print_tool_call(event.name, event.input)


def _get_stream_printer(console: UIProtocol | None) -> StreamProtocol:
    if console is None:
        return StreamPrinter()

    get_stream_printer = getattr(console, "get_stream_printer", None)
    if callable(get_stream_printer):
        return cast(StreamProtocol, get_stream_printer())

    # Backward compatibility for older console duck types that exposed `.console`
    # but not a `get_stream_printer()` hook.
    return StreamPrinter(getattr(console, "console", None))


def _tool_result(
    tool_use_id: str,
    output: str | list[dict[str, Any]] | Any,
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    """Wrap a handler output into a tool_result block.

    The ``output`` may be:
    - ``str`` — used as-is.
    - ``list[dict]`` — passed through verbatim (multimodal MCP path: text + image blocks).
    - anything else — coerced via :func:`stringify`.
    """
    content: Any
    if isinstance(output, list):
        content = output
    else:
        content = stringify(output)
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        result["is_error"] = True
    return result


def _provider_info(provider: BaseLLMProvider) -> dict[str, Any]:
    info: dict[str, Any] = {"provider_type": type(provider).__name__}
    for name in ("model", "base_url", "wire_api"):
        value = getattr(provider, name, None)
        if value not in {None, ""}:
            info[name] = value
    return info


def _serialize_tool_calls(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {
            "id": tool_call.id,
            "name": tool_call.name,
            "input": tool_call.input,
        }
        for tool_call in tool_calls
    ]


def _resolve_hook_session_id(compact_fn: Any) -> str:
    """Best-effort session id for hook JSON payloads.

    Reuses the ``get_session_id`` attribute the REPL attaches to ``compact_fn``
    (see ``main.py:_build_loop_compact``) rather than threading a new parameter
    through every caller. Falls back to ``"default"`` when unavailable (tests,
    sub-agents) — hooks don't run for sub-agents anyway.
    """
    getter = getattr(compact_fn, "get_session_id", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return "default"
    return "default"


def _run_background(bg_manager: Any, messages: list[dict[str, Any]]) -> None:
    if bg_manager is None:
        return
    inject_notifications(messages, bg_manager)


def _requires_confirmation(permission: Any, call: ToolCall) -> bool:
    if permission is None:
        return True

    requires_confirm = getattr(permission, "requires_confirm", None)
    if callable(requires_confirm):
        return bool(requires_confirm(call.name, call.input))
    return True


def _ask_permission(permission: Any, call: ToolCall) -> bool:
    ask_user = getattr(permission, "ask_user", None)
    if callable(ask_user):
        return bool(ask_user(call))
    return False


def _safe_log_request(
    *,
    interaction_logger: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    provider: BaseLLMProvider,
    console: UIProtocol | None,
) -> tuple[int | None, float]:
    if interaction_logger is None:
        return None, 0.0

    try:
        log_seq = interaction_logger.log_request(
            messages,
            tools,
            provider_info=_provider_info(provider),
        )
    except Exception as exc:
        _report_log_failure(console, "request", exc)
        return None, 0.0

    return log_seq, time.monotonic()


def _safe_log_response(
    *,
    interaction_logger: Any,
    log_seq: int | None,
    console: UIProtocol | None,
    **kwargs: Any,
) -> None:
    if interaction_logger is None or log_seq is None:
        return

    try:
        interaction_logger.log_response(log_seq, **kwargs)
    except Exception as exc:
        _report_log_failure(console, "response", exc)


def _report_log_failure(
    console: UIProtocol | None,
    phase: str,
    exc: Exception,
) -> None:
    if console is None:
        return

    console.print_status(
        f"Debug logging failed during {phase} capture ({type(exc).__name__}: {exc})."
    )
