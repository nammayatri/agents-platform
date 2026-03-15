"""Inline plan card widget with approve/reject buttons."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Static


class PlanCard(Widget):
    """Displays a proposed plan with subtasks and approve/reject actions."""

    approved = reactive(False)

    class Approved(Message):
        """Emitted when user approves the plan."""

    class Rejected(Message):
        """Emitted when user rejects the plan with feedback."""

        def __init__(self, feedback: str) -> None:
            self.feedback = feedback
            super().__init__()

    def __init__(
        self,
        plan_data: dict,
        plan_title: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.plan_data = plan_data
        self.plan_title = plan_title
        self._showing_reject_input = False

    def compose(self) -> ComposeResult:
        tasks = self.plan_data.get("tasks", [])
        if not tasks:
            tasks = self.plan_data.get("sub_tasks", [])

        title = self.plan_title or self.plan_data.get("title", "Proposed Plan")
        yield Static(f"[bold yellow]{title}[/bold yellow]", classes="plan-title")

        if self.plan_data.get("summary"):
            yield Static(
                f"[dim]{self.plan_data['summary']}[/dim]",
                classes="plan-subtask",
            )

        for i, task in enumerate(tasks):
            role = task.get("agent_role", task.get("role", ""))
            task_title = task.get("title", "")
            desc = task.get("description", "")
            deps = task.get("depends_on", [])

            role_str = f"[dim]({role})[/dim] " if role else ""
            dep_str = ""
            if deps:
                dep_refs = ", ".join(f"#{d + 1}" for d in deps)
                dep_str = f" [dim]-> {dep_refs}[/dim]"

            yield Static(
                f"  {i + 1}. {role_str}{task_title}{dep_str}",
                classes="plan-subtask",
            )

            # Show nested subtasks if present
            subtasks = task.get("sub_tasks", task.get("subtasks", []))
            for j, st in enumerate(subtasks):
                st_role = st.get("agent_role", st.get("role", ""))
                st_role_str = f"[dim]({st_role})[/dim] " if st_role else ""
                yield Static(
                    f"     {i + 1}.{j + 1}. {st_role_str}{st.get('title', '')}",
                    classes="plan-subtask",
                )

        yield Horizontal(
            Button("Approve", variant="success", id="btn-plan-approve", classes="btn-approve"),
            Button("Reject", variant="error", id="btn-plan-reject", classes="btn-reject"),
            id="plan-actions",
        )

        yield Static("", id="plan-approved-badge", classes="plan-approved-badge")
        self.query_one("#plan-approved-badge", Static).display = False

        yield Horizontal(
            Input(placeholder="Why? What should change?", id="reject-feedback"),
            Button("Send", variant="warning", id="btn-send-reject"),
            id="reject-input-area",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.approved:
            return

        if event.button.id == "btn-plan-approve":
            self._do_approve()
        elif event.button.id == "btn-plan-reject":
            self._show_reject_input()
        elif event.button.id == "btn-send-reject":
            feedback_input = self.query_one("#reject-feedback", Input)
            feedback = feedback_input.value.strip()
            if feedback:
                self._do_reject(feedback)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "reject-feedback":
            feedback = event.value.strip()
            if feedback:
                self._do_reject(feedback)

    def _do_approve(self) -> None:
        self.approved = True
        actions = self.query_one("#plan-actions", Horizontal)
        actions.display = False
        reject_area = self.query_one("#reject-input-area", Horizontal)
        reject_area.display = False
        badge = self.query_one("#plan-approved-badge", Static)
        badge.update("[bold green]Approved[/bold green]")
        badge.display = True
        self.post_message(self.Approved())

    def _show_reject_input(self) -> None:
        reject_area = self.query_one("#reject-input-area", Horizontal)
        reject_area.display = True
        self.query_one("#reject-feedback", Input).focus()

    def _do_reject(self, feedback: str) -> None:
        actions = self.query_one("#plan-actions", Horizontal)
        actions.display = False
        reject_area = self.query_one("#reject-input-area", Horizontal)
        reject_area.display = False
        badge = self.query_one("#plan-approved-badge", Static)
        badge.update(f"[yellow]Rejected -- re-planning...[/yellow]")
        badge.display = True
        self.post_message(self.Rejected(feedback))
