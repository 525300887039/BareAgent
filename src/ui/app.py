from __future__ import annotations

from typing import Any, Callable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from src.provider.base import BaseLLMProvider
from src.ui.protocol import StreamProtocol
from src.ui.widgets import ChatView, InputBar


class BareAgentApp(App):
    """BareAgent Textual application shell."""

    CSS_PATH = "styles/app.tcss"

    BINDINGS = [
        Binding("ctrl+z", "quit", "Exit", show=True),
        Binding("shift+tab", "cycle_mode", "Mode", show=True, priority=True),
        Binding("f1", "show_help", "Help", show=True),
    ]

    def __init__(
        self,
        *,
        config: Any | None = None,
        provider: BaseLLMProvider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.provider = provider
        self._messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._permission: Any = None
        self._compact_fn: Callable[[list[dict[str, Any]]], None] | None = None
        self._bg_manager: Any = None
        self._textual_ui: TextualUI | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield ChatView(id="chat")
        yield InputBar(id="input", placeholder="bareagent> ")
        yield Footer()

    def on_mount(self) -> None:
        self._textual_ui = TextualUI(self)
        self.query_one("#input", InputBar).focus()

    def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        text = event.value
        chat = self.query_one("#chat", ChatView)
        chat.append_user(text)

        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        input_bar = self.query_one("#input", InputBar)
        input_bar.disabled = True
        self.run_agent_turn(text)

    @work(thread=True, exclusive=True)
    def run_agent_turn(self, user_text: str) -> None:
        from src.core.loop import LLMCallError, agent_loop

        snapshot_len = len(self._messages)
        self._messages.append({"role": "user", "content": user_text})

        try:
            if self.provider is None:
                raise RuntimeError("Provider is not configured for the Textual app yet.")
            if self._textual_ui is None:
                raise RuntimeError("Textual UI bridge is not initialized.")

            agent_loop(
                provider=self.provider,
                messages=self._messages,
                tools=self._tools,
                handlers=self._handlers,
                permission=self._permission,
                compact_fn=self._compact_fn,
                bg_manager=self._bg_manager,
                stream=self._stream_enabled(),
                console=self._textual_ui,
            )
        except LLMCallError:
            del self._messages[snapshot_len:]

            def _do() -> None:
                self.query_one("#chat", ChatView).append_error(
                    "LLM call failed, please try again."
                )

            self.call_from_thread(_do)
        except Exception as exc:
            del self._messages[snapshot_len:]
            error_message = f"Error: {type(exc).__name__}: {exc}"

            def _do() -> None:
                self.query_one("#chat", ChatView).append_error(error_message)

            self.call_from_thread(_do)
        finally:
            self.call_from_thread(self._enable_input)

    def _enable_input(self) -> None:
        input_bar = self.query_one("#input", InputBar)
        input_bar.disabled = False
        input_bar.focus()

    def _stream_enabled(self) -> bool:
        ui_config = getattr(self.config, "ui", None)
        return bool(getattr(ui_config, "stream", False))

    def _handle_slash_command(self, text: str) -> None:
        chat = self.query_one("#chat", ChatView)

        if text == "/help":
            chat.append_status(
                "Commands: /help /exit /clear /new /compact "
                "/default /auto /plan /bypass /mode "
                "/sessions /resume /team"
            )
            return

        if text == "/exit":
            self.exit()
            return

        if text in {"/clear", "/new"}:
            self._messages.clear()
            chat.remove_children()
            chat.append_status("New conversation started.")
            return

        if text in {
            "/compact",
            "/default",
            "/auto",
            "/plan",
            "/bypass",
            "/mode",
            "/sessions",
            "/resume",
            "/team",
        }:
            chat.append_status(f"{text} will be wired in Step 3.")
            return

        chat.append_status(f"Unknown command: {text}")

    def action_cycle_mode(self) -> None:
        self.query_one("#chat", ChatView).append_status(
            "Permission mode cycling will be wired in Step 3."
        )

    def action_show_help(self) -> None:
        self.query_one("#chat", ChatView).append_status(
            "Ctrl+Z Exit | Shift+Tab Mode | /help Commands"
        )


class TextualUI:
    """UIProtocol adapter that marshals updates onto the Textual thread."""

    def __init__(self, app: BareAgentApp) -> None:
        self._app = app

    def print_assistant(self, text: str) -> None:
        def _do() -> None:
            self._app.query_one("#chat", ChatView).append_assistant_markdown(text)

        self._app.call_from_thread(_do)

    def print_tool_call(self, name: str, input_data: Any) -> None:
        def _do() -> None:
            self._app.query_one("#chat", ChatView).append_tool_call(name, input_data)

        self._app.call_from_thread(_do)

    def print_tool_result(self, name: str, output: Any) -> None:
        def _do() -> None:
            self._app.query_one("#chat", ChatView).append_tool_result(name, output)

        self._app.call_from_thread(_do)

    def print_error(self, msg: str) -> None:
        def _do() -> None:
            self._app.query_one("#chat", ChatView).append_error(msg)

        self._app.call_from_thread(_do)

    def print_status(self, msg: str) -> None:
        def _do() -> None:
            self._app.query_one("#chat", ChatView).append_status(msg)

        self._app.call_from_thread(_do)

    def get_stream_printer(self) -> StreamProtocol:
        return TextualStreamPrinter(self._app)


class TextualStreamPrinter:
    """StreamProtocol adapter backed by ChatView."""

    def __init__(self, app: BareAgentApp) -> None:
        self._app = app
        self._active = False

    def start(self) -> None:
        if self._active:
            return

        def _do() -> None:
            self._app.query_one("#chat", ChatView).begin_stream()

        self._app.call_from_thread(_do)
        self._active = True

    def feed(self, token: str) -> None:
        if not token:
            return
        if not self._active:
            self.start()

        def _do() -> None:
            self._app.query_one("#chat", ChatView).feed_stream(token)

        self._app.call_from_thread(_do)

    def finish(self) -> str:
        if not self._active:
            return ""

        def _do() -> str:
            return self._app.query_one("#chat", ChatView).end_stream_and_return()

        result = self._app.call_from_thread(_do)
        self._active = False
        return result
