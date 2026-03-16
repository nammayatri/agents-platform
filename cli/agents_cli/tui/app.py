"""Main Textual chat application."""

from __future__ import annotations

import asyncio
import json

import websockets
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from agents_cli import client, config
from agents_cli.tui.chat_log import ChatLog
from agents_cli.tui.message_input import MessageInput
from agents_cli.tui.plan_card import PlanCard
from agents_cli.tui.task_panel import TaskPanel

class ChatApp(App):
    """Agent Platform interactive chat TUI."""

    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+p", "toggle_plan", "Toggle Plan Mode", show=True),
        Binding("ctrl+t", "toggle_tasks", "Toggle Tasks", show=True),
    ]

    def __init__(
        self,
        project_id: str,
        project_name: str,
        session_id: str,
        plan_mode: bool,
        messages: list[dict] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.project_name = project_name
        self.session_id = session_id
        self.plan_mode = plan_mode
        self._initial_messages = messages or []
        self._ws_stop = asyncio.Event()
        self._task_refresh_timer = None

    def compose(self) -> ComposeResult:
        mode_text = (
            "[bold yellow]PLAN[/bold yellow]"
            if self.plan_mode
            else "[bold #6366f1]CHAT[/bold #6366f1]"
        )
        yield Static(
            f" {self.project_name}  {mode_text}  "
            "[dim]ctrl+p: mode  ctrl+t: tasks  ctrl+q: quit[/dim]",
            id="header-bar",
        )
        with Horizontal(id="main-area"):
            yield ChatLog(id="chat-log")
            yield TaskPanel(self.project_id)
        yield MessageInput()

    def on_mount(self) -> None:
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)
        msg_input.plan_mode = self.plan_mode

        # Load existing messages
        for msg in self._initial_messages:
            chat_log.add_message(
                msg.get("role", "system"),
                msg.get("content", ""),
                msg.get("metadata_json"),
            )

        msg_input.focus_input()

    # ── Keybindings ──

    def action_toggle_plan(self) -> None:
        try:
            result = client.post(
                f"/projects/{self.project_id}/chat/sessions/{self.session_id}/toggle-plan"
            )
            self.plan_mode = result.get("plan_mode", not self.plan_mode)
        except Exception:
            self.plan_mode = not self.plan_mode

        self.query_one(MessageInput).plan_mode = self.plan_mode
        self._update_header()

        chat_log = self.query_one(ChatLog)
        mode_name = "PLAN" if self.plan_mode else "CHAT"
        chat_log.add_notice(
            f"[dim]Switched to {mode_name} mode[/dim]", "yellow"
        )

    def action_toggle_tasks(self) -> None:
        panel = self.query_one(TaskPanel)
        panel.visible = not panel.visible

        if panel.visible and self._task_refresh_timer is None:
            self._task_refresh_timer = self.set_interval(
                10, self._refresh_tasks
            )
        elif not panel.visible and self._task_refresh_timer is not None:
            self._task_refresh_timer.stop()
            self._task_refresh_timer = None

    def _refresh_tasks(self) -> None:
        panel = self.query_one(TaskPanel)
        if panel.visible:
            panel.refresh_tasks()

    def _update_header(self) -> None:
        header = self.query_one("#header-bar", Static)
        mode_text = (
            "[bold yellow]PLAN[/bold yellow]"
            if self.plan_mode
            else "[bold #6366f1]CHAT[/bold #6366f1]"
        )
        header.update(
            f" {self.project_name}  {mode_text}  "
            "[dim]ctrl+p: mode  ctrl+t: tasks  ctrl+q: quit[/dim]"
        )

    # ── Message sending ──

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        content = event.content
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)

        # Show user message immediately
        chat_log.add_user_message(content)
        msg_input.sending = True
        chat_log.show_thinking()

        # Send in background
        self._send_worker = self.run_worker(
            self._do_send(content), thread=False, name="send_message"
        )

        # Start WebSocket listener for activity
        self._ws_stop.clear()
        self.run_worker(
            self._ws_listener(), thread=False, name="ws_listener"
        )

    async def _do_send(self, content: str) -> dict | None:
        """Send message via HTTP in a thread."""
        import concurrent.futures

        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(
                pool,
                lambda: client.post(
                    f"/projects/{self.project_id}/chat/sessions/{self.session_id}/messages",
                    {"content": content},
                    timeout=120.0,
                ),
            )
        return result

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "send_message":
            return

        if event.state == WorkerState.SUCCESS:
            self._on_send_complete(event.worker.result)
        elif event.state in (WorkerState.ERROR, WorkerState.CANCELLED):
            self._on_send_error(event.worker.error)

    def _on_send_complete(self, result: dict | None) -> None:
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)

        self._ws_stop.set()
        chat_log.hide_thinking()
        msg_input.sending = False

        if result:
            assistant_msg = result.get("assistant_message", {})
            chat_log.add_message(
                "assistant",
                assistant_msg.get("content", ""),
                assistant_msg.get("metadata_json"),
            )

        msg_input.focus_input()

        # Refresh tasks if panel is open
        panel = self.query_one(TaskPanel)
        if panel.visible:
            panel.refresh_tasks()

    def _on_send_error(self, error: BaseException | None) -> None:
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)

        self._ws_stop.set()
        chat_log.hide_thinking()
        msg_input.sending = False

        detail = str(error) if error else "Unknown error"
        chat_log.add_notice(f"[red]Error: {detail}[/red]", "red")
        msg_input.focus_input()

    # ── WebSocket activity listener ──

    async def _ws_listener(self) -> None:
        api_url = config.get_api_url()
        ws_url = api_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        ws_url = f"{ws_url}/ws/chat/sessions/{self.session_id}"

        chat_log = self.query_one(ChatLog)

        try:
            async with websockets.connect(ws_url) as ws:
                while not self._ws_stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        data = json.loads(raw)
                        if data.get("type") in ("tool_call", "activity"):
                            tool = data.get("tool") or data.get(
                                "activity", ""
                            )
                            status = data.get("status", "")
                            if tool:
                                label = (
                                    f"{tool}..."
                                    if status == "running"
                                    else tool
                                )
                                chat_log.update_thinking(
                                    f"Thinking... ({label})"
                                )
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            pass

    # ── Plan card actions ──

    def on_plan_card_approved(self, event: PlanCard.Approved) -> None:
        """User approved a plan from the inline card."""
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)

        chat_log.add_user_message("Looks good, go ahead and create the tasks.")
        msg_input.sending = True
        chat_log.show_thinking("Creating tasks...")

        self._ws_stop.clear()
        self.run_worker(
            self._do_send("Looks good, go ahead and create the tasks."),
            thread=False,
            name="send_message",
        )
        self.run_worker(
            self._ws_listener(), thread=False, name="ws_listener"
        )

    def on_plan_card_rejected(self, event: PlanCard.Rejected) -> None:
        """User rejected a plan with feedback."""
        feedback = event.feedback
        chat_log = self.query_one(ChatLog)
        msg_input = self.query_one(MessageInput)

        chat_log.add_user_message(feedback)
        msg_input.sending = True
        chat_log.show_thinking("Re-planning...")

        self._ws_stop.clear()
        self.run_worker(
            self._do_send(feedback), thread=False, name="send_message"
        )
        self.run_worker(
            self._ws_listener(), thread=False, name="ws_listener"
        )

    # ── Task panel actions ──

    def on_task_panel_task_action(self, event: TaskPanel.TaskAction) -> None:
        """Handle task actions from the sidebar."""
        chat_log = self.query_one(ChatLog)
        action = event.action
        task_id = event.task_id

        try:
            if action == "approve":
                result = client.post(f"/todos/{task_id}/approve-plan")
                chat_log.add_notice(
                    f"[green]Plan approved -- task is now '{result['state']}'[/green]",
                    "green",
                )
            elif action == "reject":
                result = client.post(
                    f"/todos/{task_id}/reject-plan",
                    {"feedback": "Needs improvement"},
                )
                chat_log.add_notice(
                    f"[yellow]Plan rejected -- sent back to '{result['state']}'[/yellow]",
                    "yellow",
                )
            elif action == "accept":
                result = client.post(
                    f"/todos/{task_id}/accept-deliverables"
                )
                chat_log.add_notice(
                    f"[green]Deliverables accepted -- task '{result['state']}'[/green]",
                    "green",
                )
            elif action == "cancel":
                client.post(f"/todos/{task_id}/cancel")
                chat_log.add_notice(
                    "[red]Task cancelled[/red]", "red"
                )

            # Refresh task panel
            panel = self.query_one(TaskPanel)
            panel.refresh_tasks()

        except Exception as e:
            chat_log.add_notice(f"[red]Action failed: {e}[/red]", "red")
