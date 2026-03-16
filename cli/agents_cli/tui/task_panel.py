"""Toggleable task sidebar panel."""

from __future__ import annotations

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Static

from agents_cli import client

STATE_ICONS = {
    "completed": "[green]v[/green]",
    "in_progress": "[cyan]>[/cyan]",
    "running": "[cyan]>[/cyan]",
    "plan_ready": "[bold yellow]*[/bold yellow]",
    "planning": "[yellow]~[/yellow]",
    "review": "[magenta]?[/magenta]",
    "failed": "[red]x[/red]",
    "cancelled": "[dim]-[/dim]",
    "intake": "[blue]i[/blue]",
    "scheduled": "[dim].[/dim]",
}

STATE_COLORS = {
    "completed": "green",
    "in_progress": "cyan",
    "running": "cyan",
    "plan_ready": "bold yellow",
    "planning": "yellow",
    "review": "magenta",
    "failed": "red",
    "cancelled": "dim",
    "intake": "blue",
    "scheduled": "dim",
}


class TaskPanel(Widget):
    """Right sidebar showing project tasks with inline actions."""

    visible = reactive(False)

    class TaskAction(Message):
        """Emitted when a task action button is pressed."""

        def __init__(self, action: str, task_id: str) -> None:
            self.action = action
            self.task_id = task_id
            super().__init__()

    def __init__(self, project_id: str, **kwargs) -> None:
        super().__init__(id="task-panel", **kwargs)
        self.project_id = project_id
        self._tasks: list[dict] = []
        self._selected_task: dict | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup("[bold]Tasks[/bold]"),
            classes="task-header",
        )
        yield Vertical(id="task-list")
        yield Horizontal(id="task-actions")

    def watch_visible(self, value: bool) -> None:
        if value:
            self.add_class("visible")
            self.refresh_tasks()
        else:
            self.remove_class("visible")

    def refresh_tasks(self) -> None:
        try:
            self._tasks = client.get(f"/projects/{self.project_id}/todos") or []
        except Exception:
            self._tasks = []
        self._render_tasks()

    def _render_tasks(self) -> None:
        task_list = self.query_one("#task-list", Vertical)
        task_list.remove_children()

        if not self._tasks:
            task_list.mount(
                Static("[dim]No tasks yet[/dim]", classes="task-row")
            )
            self._update_actions(None)
            return

        for task in self._tasks:
            state = task.get("state", "?")
            icon = STATE_ICONS.get(state, "[dim]?[/dim]")
            color = STATE_COLORS.get(state, "white")
            title = task.get("title", "Untitled")
            if len(title) > 22:
                title = title[:20] + ".."
            tid = task["id"][:6]

            row = Static(
                Text.from_markup(
                    f" {icon} [{color}]{state:<12}[/{color}] {title} [dim]({tid})[/dim]"
                ),
                classes="task-row",
                id=f"task-{task['id'][:8]}",
            )
            row._task_data = task
            task_list.mount(row)

        # Auto-select first actionable task
        actionable = [
            t
            for t in self._tasks
            if t.get("state") in ("plan_ready", "review")
        ]
        if actionable:
            self._update_actions(actionable[0])
        else:
            self._update_actions(None)

    def _update_actions(self, task: dict | None) -> None:
        self._selected_task = task
        actions = self.query_one("#task-actions", Horizontal)
        actions.remove_children()

        if task is None:
            return

        state = task.get("state", "")
        if state == "plan_ready":
            actions.mount(
                Button(
                    "Approve",
                    variant="success",
                    id="btn-task-approve",
                    classes="btn-approve",
                )
            )
            actions.mount(
                Button(
                    "Reject",
                    variant="error",
                    id="btn-task-reject",
                    classes="btn-reject",
                )
            )
        elif state == "review":
            actions.mount(
                Button(
                    "Accept",
                    variant="success",
                    id="btn-task-accept",
                    classes="btn-approve",
                )
            )

        # Cancel is always available for active tasks
        if state in ("in_progress", "planning", "intake", "plan_ready"):
            actions.mount(
                Button(
                    "Cancel",
                    variant="warning",
                    id="btn-task-cancel",
                )
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._selected_task is None:
            return

        tid = self._selected_task["id"]
        btn_id = event.button.id

        if btn_id == "btn-task-approve":
            self.post_message(self.TaskAction("approve", tid))
        elif btn_id == "btn-task-reject":
            self.post_message(self.TaskAction("reject", tid))
        elif btn_id == "btn-task-accept":
            self.post_message(self.TaskAction("accept", tid))
        elif btn_id == "btn-task-cancel":
            self.post_message(self.TaskAction("cancel", tid))

    def on_click(self, event) -> None:
        # Check if a task row was clicked
        try:
            widget = self.screen.get_widget_at(event.screen_x, event.screen_y)[0]
            if hasattr(widget, "_task_data"):
                self._update_actions(widget._task_data)
        except Exception:
            pass
