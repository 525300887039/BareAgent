from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

import src.main as main_module
from src.concurrency.background import BackgroundManager
from src.core.loop import LLMCallError, agent_loop
from src.core.tools import get_tools
from src.memory.compact import Compactor
from src.memory.transcript import TranscriptManager
from src.permission.guard import PermissionGuard
from src.planning.skills import SkillLoader, resolve_skills_dir
from src.planning.tasks import TaskManager
from src.planning.todo import TodoManager
from src.provider.base import BaseLLMProvider
from src.team.autonomous import AutonomousAgent
from src.team.mailbox import MessageBus
from src.team.manager import TeammateManager
from src.ui.protocol import StreamProtocol
from src.ui.widgets import ChatView, InputBar, PermissionModal

_HELP_TEXT = (
    main_module._HELP_TEXT + "\n"
    "  Shift+Tab  Cycle through permission modes"
)


def make_ask_via_modal(app: BareAgentApp) -> Callable[[Any], bool]:
    """Create a permission callback that blocks until the modal is dismissed."""

    def _ask(call: Any) -> bool:
        event = threading.Event()
        result = [False]

        def on_dismiss(allowed: bool | None) -> None:
            result[0] = bool(allowed)
            event.set()

        app.call_from_thread(
            app.push_screen,
            PermissionModal(call.name, call.input),
            on_dismiss,
        )
        event.wait()
        return result[0]

    return _ask


class _ChatViewAsConsole:
    """Adapt ChatView to the subset of AgentConsole used by team helpers."""

    def __init__(self, chat: ChatView) -> None:
        self._chat = chat
        self.console = self

    def print(self, *args: Any, **kwargs: Any) -> None:
        _ = kwargs
        self._chat.append_status(" ".join(str(arg) for arg in args))

    def print_status(self, msg: str) -> None:
        self._chat.append_status(msg)

    def print_error(self, msg: str) -> None:
        self._chat.append_error(msg)


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
        config: Any,
        provider: BaseLLMProvider,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.provider = provider
        self._workspace: Path = Path.cwd().resolve()
        self._transcript_mgr: TranscriptManager | None = None
        self._session_id: str = ""
        self._todo_manager: TodoManager | None = None
        self._task_manager: TaskManager | None = None
        self._bg_manager: BackgroundManager | None = None
        self._teammate_manager: TeammateManager | None = None
        self._skill_loader: SkillLoader | None = None
        self._message_bus: MessageBus | None = None
        self._mailbox_cursor: str | None = None
        self._spawned_agents: dict[str, AutonomousAgent] = {}
        self._messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._permission: PermissionGuard | None = None
        self._compact_fn: Callable[..., Any] | None = None
        self._textual_ui: TextualUI | None = None
        self._pending_mode_select = False
        self._chat_console: _ChatViewAsConsole | None = None
        self._interaction_logger: Any = None
        self._viewer_server: Any = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield ChatView(id="chat")
        yield InputBar(id="input", placeholder="bareagent> ")
        yield Footer()

    def on_mount(self) -> None:
        from src.ui.theme import init_theme

        init_theme(self.config.ui.theme)
        chat = self.query_one("#chat", ChatView)
        self._chat_console = _ChatViewAsConsole(chat)
        startup_console = self._chat_console
        self._workspace = Path.cwd().resolve()
        self._transcript_mgr = TranscriptManager(self._workspace / ".transcripts")
        self._session_id = main_module._generate_session_id(self._transcript_mgr)
        self._interaction_logger = main_module._build_interaction_logger(
            self.config,
            self._workspace,
            self._session_id,
        )
        self._todo_manager = TodoManager()
        self._task_manager = main_module._load_task_manager(
            self._workspace,
            startup_console,
        )
        self._bg_manager = BackgroundManager()
        self._teammate_manager = main_module._load_teammate_manager(
            self._workspace,
            startup_console,
        )
        self._skill_loader = SkillLoader(resolve_skills_dir())
        self._message_bus, self._mailbox_cursor = main_module._switch_session_mailbox(
            self._workspace,
            self._session_id,
        )
        self._spawned_agents = {}
        self._messages = main_module._initial_messages(
            self._workspace,
            skill_summary=self._skill_loader.get_skill_list_prompt(),
        )
        self._tools = get_tools()
        self._permission = main_module._build_permission_guard(self.config)
        self._permission._ask_user_fn = make_ask_via_modal(self)
        base_compact_fn = Compactor(
            provider=self.provider,
            transcript_mgr=self._transcript_mgr,
            session_id=self._session_id,
        )
        self._compact_fn = main_module._build_loop_compact(
            base_compact_fn,
            self._todo_manager,
        )
        self._textual_ui = TextualUI(self)
        self._rebuild_handlers(runtime_id=self._session_id)
        self.title = "BareAgent"
        self._update_subtitle()

        chat.append_status(
            f"BareAgent REPL ({self.config.provider.name}/{self.config.provider.model})"
        )
        chat.append_status(
            "Permission mode: "
            f"{self._permission.mode.value}. Type /help for commands."
        )
        self.query_one("#input", InputBar).focus()

    def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        text = event.value
        event.input.value = ""
        self._drain_team_messages()
        chat = self.query_one("#chat", ChatView)
        chat.append_user(text)

        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        if self._pending_mode_select:
            self._pending_mode_select = False
            self._handle_mode_selection(text)
            return

        input_bar = self.query_one("#input", InputBar)
        input_bar.disabled = True
        self.run_agent_turn(text)

    @work(thread=True, exclusive=True)
    def run_agent_turn(self, user_text: str) -> None:
        self._messages.append({"role": "user", "content": user_text})
        snapshot_len = len(self._messages) - 1

        try:
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
                interaction_logger=self._interaction_logger,
            )
            self._save_transcript_snapshot()
            self.call_from_thread(self._drain_team_messages)
        except LLMCallError:
            del self._messages[snapshot_len:]
            self._app_call(self._append_error, "LLM call failed, please try again.")
        except KeyboardInterrupt:
            del self._messages[snapshot_len:]
            self._app_call(self._append_status, "Agent loop interrupted.")
        except Exception as exc:
            del self._messages[snapshot_len:]
            self._app_call(
                self._append_error,
                f"Error: {type(exc).__name__}: {exc}",
            )
        finally:
            self.call_from_thread(self._enable_input)

    def _enable_input(self) -> None:
        input_bar = self.query_one("#input", InputBar)
        input_bar.disabled = False
        input_bar.focus()

    def _stream_enabled(self) -> bool:
        ui_config = getattr(self.config, "ui", None)
        return bool(getattr(ui_config, "stream", False))

    def _append_status(self, message: str) -> None:
        self.query_one("#chat", ChatView).append_status(message)

    def _append_error(self, message: str) -> None:
        self.query_one("#chat", ChatView).append_error(message)

    def _handle_slash_command(self, text: str) -> None:
        chat = self.query_one("#chat", ChatView)

        if text == "/exit":
            self._pending_mode_select = False
            main_module._broadcast_team_shutdown(self._message_bus)
            self.exit()
            return

        if text == "/help":
            self._pending_mode_select = False
            chat.append_status(_HELP_TEXT)
            return

        if text in {"/clear", "/new"}:
            self._pending_mode_select = False
            self._messages[:] = main_module._initial_messages(
                self._workspace,
                skill_summary=self._skill_loader.get_skill_list_prompt(),
            )
            self._todo_manager.reset()
            new_session_id = main_module._generate_session_id(
                self._transcript_mgr,
                reserved_ids={main_module._get_compact_session_id(self._compact_fn)},
            )
            main_module._set_compact_session_id(self._compact_fn, new_session_id)
            self._session_id = new_session_id
            main_module._set_interaction_logger_session(
                self._interaction_logger,
                new_session_id,
            )
            self._message_bus, self._mailbox_cursor = main_module._switch_session_mailbox(
                self._workspace,
                new_session_id,
                current_bus=self._message_bus,
            )
            self._spawned_agents = {}
            self._rebuild_handlers(runtime_id=new_session_id)
            chat.remove_children()
            chat.append_status("New conversation started.")
            return

        if text == "/compact":
            self._pending_mode_select = False
            self._compact_fn(self._messages, force=True)
            self._save_transcript_snapshot()
            self._rebuild_handlers(
                runtime_id=main_module._get_compact_session_id(self._compact_fn)
            )
            chat.append_status("Context compaction finished.")
            return

        if text == "/sessions":
            self._pending_mode_select = False
            sessions = self._transcript_mgr.list_sessions()
            if not sessions:
                chat.append_status("No saved sessions.")
                return
            for saved_session in sessions:
                chat.append_status(saved_session)
            return

        if text == "/resume" or text.startswith("/resume "):
            self._pending_mode_select = False
            _, _, raw_id = text.partition(" ")
            requested = raw_id.strip() or None
            try:
                restored = self._transcript_mgr.resume(requested)
            except FileNotFoundError as exc:
                chat.append_error(str(exc))
                return

            self._messages[:] = restored
            resumed = requested or self._transcript_mgr.get_latest_session()
            if resumed is not None:
                self._session_id = resumed
                main_module._set_compact_session_id(self._compact_fn, resumed)
                main_module._set_interaction_logger_session(
                    self._interaction_logger,
                    resumed,
                )
                self._message_bus, self._mailbox_cursor = main_module._switch_session_mailbox(
                    self._workspace,
                    resumed,
                    current_bus=self._message_bus,
                )
                self._spawned_agents = {}
            self._rebuild_handlers(
                runtime_id=main_module._get_compact_session_id(self._compact_fn)
            )
            self._render_chat_history()
            chat.append_status(f"Resumed session: {resumed}")
            return

        if text == "/log" or text.startswith("/log "):
            self._pending_mode_select = False
            self._viewer_server = main_module._handle_log_command(
                text,
                config=self.config,
                interaction_logger=self._interaction_logger,
                viewer_server=self._viewer_server,
                print_status=chat.append_status,
            )
            return

        if text in main_module._PERMISSION_SLASH:
            self._pending_mode_select = False
            old = self._permission.mode
            self._permission.mode = main_module._PERMISSION_SLASH[text]
            chat.append_status(
                f"Permission mode: {old.value} → {self._permission.mode.value}"
            )
            self._update_subtitle()
            return

        if text == "/theme" or text.startswith("/theme "):
            self._pending_mode_select = False
            _, _, theme_arg = text.partition(" ")
            theme_name = theme_arg.strip()

            from src.ui.theme import format_theme_list, format_unknown_theme, get_theme

            tm = get_theme()
            if not theme_name:
                chat.append_status(format_theme_list(tm))
                return

            if tm.switch(theme_name):
                chat.rerender_transcript()
                chat.append_status(f"Theme switched to: {theme_name}")
            else:
                chat.append_error(format_unknown_theme(theme_name))
            return

        if text == "/mode":
            self._pending_mode_select = True
            lines = ["Permission modes:"]
            for idx, mode in enumerate(main_module._MODE_CYCLE, 1):
                marker = "*" if mode == self._permission.mode else " "
                lines.append(
                    f"  {marker} {idx}) {mode.value:<10} "
                    f"{main_module._MODE_DESCRIPTIONS[mode]}"
                )
            lines.append(f"Type 1-{len(main_module._MODE_CYCLE)} to select.")
            chat.append_status("\n".join(lines))
            return

        if text == "/team" or text.startswith("/team "):
            self._pending_mode_select = False
            main_module._handle_team_command(
                text,
                self._chat_console,
                teammate_manager=self._teammate_manager,
                team_handlers=self._handlers,
            )
            return

        self._pending_mode_select = False
        chat.append_status(f"Unknown command: {text}")

    def _handle_mode_selection(self, text: str) -> None:
        chat = self.query_one("#chat", ChatView)
        choices = {
            str(index): mode
            for index, mode in enumerate(main_module._MODE_CYCLE, 1)
        }
        selected = choices.get(text.strip())
        if selected is None:
            chat.append_status("Invalid choice, mode unchanged.")
            return

        old = self._permission.mode
        self._permission.mode = selected
        chat.append_status(
            f"Permission mode: {old.value} → {self._permission.mode.value}"
        )
        self._update_subtitle()

    def action_cycle_mode(self) -> None:
        old = self._permission.mode
        index = main_module._MODE_CYCLE.index(self._permission.mode)
        self._permission.mode = main_module._MODE_CYCLE[
            (index + 1) % len(main_module._MODE_CYCLE)
        ]
        self.query_one("#chat", ChatView).append_status(
            f"Permission mode: {old.value} → {self._permission.mode.value}"
        )
        self._update_subtitle()

    def action_show_help(self) -> None:
        self.query_one("#chat", ChatView).append_status(_HELP_TEXT)

    def _update_subtitle(self) -> None:
        self.sub_title = (
            f"{self.config.provider.name}/{self.config.provider.model} "
            f"[{self._permission.mode.value.upper()}]"
        )

    def _rebuild_handlers(self, *, runtime_id: str) -> None:
        self._handlers = main_module._build_handlers(
            workspace_path=self._workspace,
            todo_manager=self._todo_manager,
            task_manager=self._task_manager,
            skill_loader=self._skill_loader,
            provider=self.provider,
            tools=self._tools,
            permission=self._permission,
            bg_manager=self._bg_manager,
            messages=self._messages,
            config=self.config,
            runtime_id=runtime_id,
            teammate_manager=self._teammate_manager,
            message_bus=self._message_bus,
            spawned_agents=self._spawned_agents,
            agent_name=main_module.MAIN_AGENT_NAME,
        )

    def _save_transcript_snapshot(self) -> None:
        main_module._save_transcript_snapshot(
            self._transcript_mgr,
            self._messages,
            self._compact_fn,
        )

    def _drain_team_messages(self) -> None:
        self._mailbox_cursor = main_module._drain_team_mailbox(
            self._chat_console,
            message_bus=self._message_bus,
            since=self._mailbox_cursor,
        )

    def _render_chat_history(self) -> None:
        chat = self.query_one("#chat", ChatView)
        chat.remove_children()
        tool_name_by_id: dict[str, str] = {}

        for message in self._messages:
            role = message.get("role")
            content = message.get("content")

            if role == "system":
                continue

            if role == "user":
                if isinstance(content, str):
                    chat.append_user(content)
                    continue
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        tool_name = tool_name_by_id.get(
                            str(block.get("tool_use_id", "")),
                            "unknown",
                        )
                        chat.append_tool_result(tool_name, block.get("content", ""))
                continue

            if role != "assistant":
                continue

            if isinstance(content, str):
                chat.append_assistant_markdown(content)
                continue
            if not isinstance(content, list):
                continue

            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                    continue
                if block_type != "tool_use":
                    continue

                tool_id = str(block.get("id", ""))
                if tool_id:
                    tool_name_by_id[tool_id] = str(block.get("name", "unknown"))

                if text_parts:
                    chat.append_assistant_markdown("\n".join(part for part in text_parts if part))
                    text_parts = []
                chat.append_tool_call(
                    str(block.get("name", "unknown")),
                    block.get("input", {}),
                )

            if text_parts:
                chat.append_assistant_markdown("\n".join(part for part in text_parts if part))

    def _app_call(self, fn: Callable[..., Any], *args: Any) -> None:
        self.call_from_thread(fn, *args)


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
