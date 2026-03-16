"""Chat message input widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.events import Key
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, TextArea


class ChatTextArea(TextArea):
    """TextArea that sends Enter as submit instead of newline.

    Shift+Enter inserts a newline. Plain Enter triggers submission.
    """

    class SendRequested(Message):
        """User pressed Enter (without Shift)."""

        def __init__(self, content: str) -> None:
            self.content = content
            super().__init__()

    async def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            # Plain Enter = send
            content = self.text.strip()
            if content and not self.read_only:
                event.prevent_default()
                event.stop()
                self.post_message(self.SendRequested(content))
                self.clear()
            else:
                event.prevent_default()
                event.stop()
            return

        if event.key == "shift+enter":
            # Shift+Enter = newline (let TextArea handle it as "enter")
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        await super()._on_key(event)


class MessageInput(Widget):
    """Multi-line input with mode indicator and send-on-Enter."""

    plan_mode = reactive(False)
    sending = reactive(False)

    class Submitted(Message):
        """Emitted when user presses Enter to send."""

        def __init__(self, content: str) -> None:
            self.content = content
            super().__init__()

    def compose(self) -> ComposeResult:
        with Horizontal(id="input-area"):
            yield Static("P >", id="mode-indicator")
            yield ChatTextArea(id="message-input")

    def on_mount(self) -> None:
        ta = self.query_one("#message-input", ChatTextArea)
        ta.show_line_numbers = False
        ta.tab_behavior = "focus"
        ta.soft_wrap = True
        self._update_mode_label()

    def watch_plan_mode(self, value: bool) -> None:
        self._update_mode_label()

    def watch_sending(self, value: bool) -> None:
        ta = self.query_one("#message-input", ChatTextArea)
        if value:
            ta.add_class("disabled")
            ta.read_only = True
        else:
            ta.remove_class("disabled")
            ta.read_only = False
            ta.focus()

    def _update_mode_label(self) -> None:
        try:
            indicator = self.query_one("#mode-indicator", Static)
            if self.plan_mode:
                indicator.update("[bold yellow]P >[/bold yellow]")
                indicator.remove_class("chat-mode")
            else:
                indicator.update("[bold #6366f1]> >[/bold #6366f1]")
                indicator.add_class("chat-mode")
        except Exception:
            pass

    def on_chat_text_area_send_requested(
        self, event: ChatTextArea.SendRequested
    ) -> None:
        self.post_message(self.Submitted(event.content))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        ta = self.query_one("#message-input", ChatTextArea)
        lines = ta.document.line_count
        ta.styles.height = min(max(lines + 1, 2), 6)

    def focus_input(self) -> None:
        self.query_one("#message-input", ChatTextArea).focus()
