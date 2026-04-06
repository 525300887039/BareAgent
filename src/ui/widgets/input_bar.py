from __future__ import annotations

from typing import Any

from textual.message import Message
from textual.suggester import SuggestFromList
from textual.widgets import Input

_SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/clear",
    "/new",
    "/compact",
    "/default",
    "/auto",
    "/plan",
    "/bypass",
    "/mode",
    "/sessions",
    "/resume",
    "/team",
]


class InputBar(Input):
    """Input widget with slash-command suggestions."""

    class Submitted(Message):
        """Custom message emitted after a trimmed submission."""

        def __init__(
            self,
            input_bar: InputBar,
            value: str,
            validation_result: Any = None,
        ) -> None:
            super().__init__()
            self.input = input_bar
            self.value = value.strip()
            self.validation_result = validation_result

    def __init__(self, **kwargs) -> None:
        super().__init__(
            suggester=SuggestFromList(_SLASH_COMMANDS, case_sensitive=False),
            **kwargs,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Forward trimmed submissions through the custom message type."""
        event.stop()
        value = self.value.strip()
        self.value = ""
        if not value:
            return
        self.post_message(self.Submitted(self, value, event.validation_result))
