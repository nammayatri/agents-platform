"""Scrollable chat message log."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.text import Text

from textual.containers import VerticalScroll
from textual.widgets import Static

from agents_cli.tui.plan_card import PlanCard


class ChatLog(VerticalScroll):
    """Scrollable container for chat messages."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._thinking_widget: Static | None = None

    def add_user_message(self, content: str) -> None:
        widget = Static(
            Text.from_markup(f"[bold blue]You[/bold blue]\n{content}"),
            classes="message-user",
        )
        self.mount(widget)
        self._scroll_to_end()

    def add_assistant_message(self, content: str) -> None:
        try:
            rendered = Markdown(content)
        except Exception:
            rendered = Text(content)

        widget = Static(rendered, classes="message-assistant")
        self.mount(widget)
        self._scroll_to_end()

    def add_system_message(self, content: str) -> None:
        widget = Static(
            Text.from_markup(f"[dim italic]{content}[/dim italic]"),
            classes="message-system",
        )
        self.mount(widget)
        self._scroll_to_end()

    def add_plan_card(self, plan_data: dict, plan_title: str = "") -> PlanCard:
        card = PlanCard(plan_data, plan_title=plan_title, classes="plan-card")
        self.mount(card)
        self._scroll_to_end()
        return card

    def add_notice(
        self, text: str, style: str = "green"
    ) -> None:
        css_class = f"notice-{style}"
        widget = Static(
            Text.from_markup(text),
            classes=css_class,
        )
        self.mount(widget)
        self._scroll_to_end()

    def show_thinking(self, text: str = "Thinking...") -> None:
        if self._thinking_widget is None:
            self._thinking_widget = Static(
                Text.from_markup(f"[dim italic]{text}[/dim italic]"),
                classes="thinking",
            )
            self.mount(self._thinking_widget)
            self._scroll_to_end()
        else:
            self._thinking_widget.update(
                Text.from_markup(f"[dim italic]{text}[/dim italic]")
            )

    def update_thinking(self, text: str) -> None:
        if self._thinking_widget is not None:
            self._thinking_widget.update(
                Text.from_markup(f"[dim italic]{text}[/dim italic]")
            )

    def hide_thinking(self) -> None:
        if self._thinking_widget is not None:
            self._thinking_widget.remove()
            self._thinking_widget = None

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        if not content:
            return

        metadata = metadata or {}
        action = metadata.get("action")

        if role == "user":
            self.add_user_message(content)
        elif role == "assistant":
            self.add_assistant_message(content)

            if action == "plan_proposed":
                plan_data = metadata.get("plan_data", {})
                plan_title = metadata.get("plan_title", "")
                self.add_plan_card(plan_data, plan_title=plan_title)
            elif action == "plan_accepted":
                count = metadata.get("tasks_created", 0)
                self.add_notice(
                    f"[bold green]Plan accepted -- {count} task(s) created[/bold green]",
                    "green",
                )
            elif action == "task_created":
                title = metadata.get("task_title", "")
                self.add_notice(
                    f"[cyan]Task created: {title}[/cyan]",
                    "green",
                )
        elif role == "system":
            self.add_system_message(content)

    def _scroll_to_end(self) -> None:
        self.call_later(self.scroll_end, animate=False)
