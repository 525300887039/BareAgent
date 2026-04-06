from __future__ import annotations

from io import StringIO

from rich.console import Console

from src.ui.console import AgentConsole
from src.ui.stream import StreamPrinter


def test_agent_console_truncates_long_tool_output() -> None:
    output_buffer = StringIO()
    console = AgentConsole(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=100)
    )

    console.print_tool_result("bash", "x" * 2100)

    rendered = output_buffer.getvalue()
    assert "[truncated]" in rendered


def test_stream_printer_accumulates_streamed_text() -> None:
    status_buffer = StringIO()
    stream_buffer = StringIO()
    printer = StreamPrinter(
        Console(file=status_buffer, force_terminal=False, color_system=None, width=80),
        writer=stream_buffer,
    )

    printer.start()
    printer.feed("Hel")
    printer.feed("lo")

    assert printer.finish() == "Hello"
    assert "Thinking..." in status_buffer.getvalue()
    assert stream_buffer.getvalue() == "Hello\n"


def test_stream_printer_defaults_to_console_file() -> None:
    output_buffer = StringIO()
    printer = StreamPrinter(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=80),
    )

    printer.start()
    printer.feed("Hello")

    assert printer.finish() == "Hello"
    rendered = output_buffer.getvalue()
    assert "Thinking..." in rendered
    assert rendered.count("Hello") == 1
    assert rendered.endswith("Hello\n")


def test_stream_printer_does_not_leak_theme_stack_on_shared_console() -> None:
    output_buffer = StringIO()
    console = Console(
        file=output_buffer,
        force_terminal=False,
        color_system=None,
        width=80,
    )
    baseline = len(console._theme_stack._entries)

    for token in ("alpha", "beta"):
        printer = StreamPrinter(console)
        printer.start()
        printer.feed(token)
        assert printer.finish() == token

    assert len(console._theme_stack._entries) == baseline
