from __future__ import annotations

import time
from typing import Any, Callable

from src.concurrency.notification import inject_notifications
from src.core.fileutil import stringify
from src.provider.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall
from src.ui.protocol import StreamProtocol, UIProtocol
from src.ui.stream import StreamPrinter


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
) -> str:
    compact = compact_fn or (lambda _messages: None)

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

        try:
            response, streamed_output, displayed_tool_calls = _invoke_provider(
                provider=provider,
                messages=messages,
                tools=tools,
                stream=stream,
                console=console,
            )
        except BaseException as exc:
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
            return response.text or ""

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

            handler = handlers.get(call.name)
            if handler is None:
                output = f"Unknown tool: {call.name}"
                if console is not None:
                    console.print_tool_result(call.name, output)
                results.append(
                    _tool_result(call.id, output, is_error=True)
                )
                continue

            try:
                output = handler(**call.input)
            except Exception as exc:
                output = f"Error: {type(exc).__name__}: {exc}"
                if console is not None:
                    console.print_tool_result(call.name, output)
                results.append(
                    _tool_result(call.id, output, is_error=True)
                )
                continue

            if console is not None:
                console.print_tool_result(call.name, output)
            results.append(_tool_result(call.id, output))

        messages.append({"role": "user", "content": results})

    msg = f"Agent loop exceeded {max_iterations} iterations"
    if console is not None:
        console.print_error(msg)
    raise LLMCallError(msg)


def _invoke_provider(
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
    exc: Exception,
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
                    raise RuntimeError("Streaming provider did not return a response.")
                return response, streamed_any_text or bool(streamed_text), displayed_tool_calls

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
        return get_stream_printer()

    # Backward compatibility for older console duck types that exposed `.console`
    # but not a `get_stream_printer()` hook.
    return StreamPrinter(getattr(console, "console", None))


def _tool_result(tool_use_id: str, output: Any, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": stringify(output),
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
