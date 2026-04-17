from __future__ import annotations

import importlib
import sys
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
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


@pytest.mark.parametrize(
    ("response", "expected", "choice"),
    [("y", True, "allow"), ("n", False, "deny"), ("a", True, "always")],
)
def test_agent_console_ask_permission_accepts_expected_answers(
    monkeypatch,
    response: str,
    expected: bool,
    choice: str,
) -> None:
    output_buffer = StringIO()
    console = AgentConsole(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=100)
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": response)

    assert console.ask_permission("bash", {"command": "pwd"}) is expected
    assert console.consume_permission_choice() == choice
    assert console.consume_permission_choice() is None

    rendered = output_buffer.getvalue()
    assert "Permission Required: bash" in rendered
    assert "[y] Allow" in rendered
    assert "[n] Deny" in rendered
    assert "[a] Always allow" in rendered
    assert '"command": "pwd"' in rendered


def test_agent_console_ask_permission_retries_after_invalid_input(
    monkeypatch,
) -> None:
    output_buffer = StringIO()
    console = AgentConsole(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=100)
    )
    responses = iter(["maybe", "a"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert console.ask_permission("bash", {"command": "pwd"}) is True
    assert console.consume_permission_choice() == "always"

    assert "Press y/n/a" in output_buffer.getvalue()


def test_agent_console_ask_permission_truncates_long_payload(monkeypatch) -> None:
    output_buffer = StringIO()
    console = AgentConsole(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=100)
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    assert console.ask_permission("bash", {"command": "x" * 700}) is False

    rendered = output_buffer.getvalue()
    assert "[truncated]" in rendered
    assert "Permission Required: bash" in rendered


@pytest.mark.parametrize("exc_type", [EOFError, KeyboardInterrupt])
def test_agent_console_ask_permission_fails_closed_on_input_interrupt(
    monkeypatch,
    exc_type: type[BaseException],
) -> None:
    output_buffer = StringIO()
    console = AgentConsole(
        Console(file=output_buffer, force_terminal=False, color_system=None, width=100)
    )

    def _raise_input(_prompt: str = "") -> str:
        raise exc_type

    monkeypatch.setattr("builtins.input", _raise_input)

    assert console.ask_permission("bash", {"command": "pwd"}) is False
    assert console.consume_permission_choice() == "deny"


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


def _load_prompt_module(monkeypatch: pytest.MonkeyPatch):
    class _FakeWordCompleter:
        def __init__(self, words, sentence: bool = False) -> None:
            self.words = list(words)
            self.sentence = sentence

    class _FakeHistory:
        pass

    class _FakeFileHistory(_FakeHistory):
        def __init__(self, filename: str) -> None:
            self.filename = filename

    class _FakeInMemoryHistory(_FakeHistory):
        pass

    class _FakeKeyBindings:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}

        def add(self, key: str):
            def _decorator(func):
                self.handlers[key] = func
                return func

            return _decorator

    class _FakePromptSession:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def prompt(self, message) -> str:
            if callable(message):
                return message()
            return message

    fake_prompt_toolkit = ModuleType("prompt_toolkit")
    fake_completion = ModuleType("prompt_toolkit.completion")
    fake_history = ModuleType("prompt_toolkit.history")
    fake_key_binding = ModuleType("prompt_toolkit.key_binding")

    fake_prompt_toolkit.PromptSession = _FakePromptSession
    fake_completion.WordCompleter = _FakeWordCompleter
    fake_history.FileHistory = _FakeFileHistory
    fake_history.History = _FakeHistory
    fake_history.InMemoryHistory = _FakeInMemoryHistory
    fake_key_binding.KeyBindings = _FakeKeyBindings

    for name in (
        "src.ui.prompt",
        "prompt_toolkit",
        "prompt_toolkit.completion",
        "prompt_toolkit.history",
        "prompt_toolkit.key_binding",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.setitem(sys.modules, "prompt_toolkit", fake_prompt_toolkit)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.completion", fake_completion)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.history", fake_history)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.key_binding", fake_key_binding)

    prompt_module = importlib.import_module("src.ui.prompt")
    prompt_module = importlib.reload(prompt_module)
    return prompt_module, _FakeInMemoryHistory


def test_agent_prompt_uses_in_memory_history_when_history_file_is_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt_module, fake_in_memory_history = _load_prompt_module(monkeypatch)
    history_file = tmp_path / ".bareagent_history"
    real_open = Path.open

    def _fake_open(self: Path, *args, **kwargs):
        if self == history_file:
            raise PermissionError("read-only")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _fake_open)

    agent_prompt = prompt_module.AgentPrompt(
        commands=["/help"],
        history_file=history_file,
        get_mode_label=lambda: "DEFAULT",
    )

    assert isinstance(agent_prompt._session.kwargs["history"], fake_in_memory_history)


def test_agent_prompt_bindings_insert_newline_and_cycle_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt_module, _fake_in_memory_history = _load_prompt_module(monkeypatch)
    cycled: list[str] = []
    inserted: list[str] = []
    invalidated: list[bool] = []

    agent_prompt = prompt_module.AgentPrompt(
        commands=["/help"],
        history_file=tmp_path / ".bareagent_history",
        get_mode_label=lambda: "DEFAULT",
        cycle_mode=lambda: cycled.append("AUTO") or "AUTO",
    )
    bindings = agent_prompt._session.kwargs["key_bindings"]
    event = SimpleNamespace(
        current_buffer=SimpleNamespace(insert_text=inserted.append),
        app=SimpleNamespace(invalidate=lambda: invalidated.append(True)),
    )

    bindings.handlers["c-j"](event)
    bindings.handlers["s-tab"](event)

    assert inserted == ["\n"]
    assert cycled == ["AUTO"]
    assert invalidated == [True]
    assert "Shift+Tab: cycle mode" in agent_prompt._toolbar()
