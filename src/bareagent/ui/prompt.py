from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings


class _ResilientFileHistory(FileHistory):
    def store_string(self, string: str) -> None:
        try:
            super().store_string(string)
        except OSError:
            # Keep the current session usable even if history persistence fails.
            pass


class AgentPrompt:
    def __init__(
        self,
        *,
        commands: list[str],
        history_file: Path,
        get_mode_label: Callable[[], str],
        cycle_mode: Callable[[], str] | None = None,
    ) -> None:
        self._get_mode_label = get_mode_label
        self._cycle_mode = cycle_mode
        self._session = PromptSession(
            completer=WordCompleter(commands, sentence=True),
            history=self._build_history(history_file),
            key_bindings=self._build_bindings(cycle_mode),
            complete_while_typing=True,
            bottom_toolbar=self._toolbar,
        )

    def read_input(self) -> str:
        return self._session.prompt(
            lambda: f"[{self._get_mode_label()}] bareagent> ",
        )

    def _toolbar(self) -> str:
        mode = self._get_mode_label()
        if self._cycle_mode is None:
            return f" Mode: {mode} | /help: commands"
        return f" Mode: {mode} | Shift+Tab: cycle mode | /help: commands"

    @staticmethod
    def _build_history(history_file: Path) -> History:
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            with history_file.open("ab"):
                pass
        except OSError:
            return InMemoryHistory()
        return _ResilientFileHistory(str(history_file))

    @staticmethod
    def _build_bindings(
        cycle_mode: Callable[[], str] | None = None,
    ) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-j")
        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        def _on_cycle_mode(event) -> None:
            if cycle_mode is None:
                return
            cycle_mode()
            event.app.invalidate()

        return bindings
