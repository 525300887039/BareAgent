from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from src.ui.theme import get_theme


class PermissionModal(ModalScreen[bool]):
    """Modal dialog for confirming tool execution."""

    BINDINGS = [
        ("y", "allow", "Allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
    }

    PermissionModal > Vertical {
        width: 72;
        max-width: 90%;
        max-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #pm-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #pm-detail {
        margin-bottom: 1;
        max-height: 10;
        overflow-y: auto;
    }

    #pm-buttons {
        width: 1fr;
        height: 3;
        align: center middle;
    }

    #pm-buttons > Button {
        margin: 0 1;
    }
    """

    def __init__(self, tool_name: str, tool_input: Any) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._tool_input = tool_input

    def compose(self) -> ComposeResult:
        tm = get_theme()
        detail = json.dumps(
            self._tool_input,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        if len(detail) > 500:
            detail = f"{detail[:500]}\n... [truncated]"

        with Vertical():
            yield Label(
                f"{tm.icons.warning} Permission Required: {self._tool_name}",
                id="pm-title",
            )
            yield Static(detail, id="pm-detail", markup=False)
            with Horizontal(id="pm-buttons"):
                yield Button("Allow (y)", id="pm-allow", variant="success")
                yield Button("Deny (n)", id="pm-deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pm-allow")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
