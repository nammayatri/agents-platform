"""Rich formatters for task/subtask data."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STATE_COLORS = {
    "scheduled": "dim",
    "intake": "blue",
    "planning": "yellow",
    "plan_ready": "bold yellow",
    "in_progress": "cyan",
    "review": "magenta",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim red",
}

PRIORITY_COLORS = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "dim",
}

SUBTASK_STATUS_COLORS = {
    "pending": "dim",
    "assigned": "blue",
    "running": "cyan",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim red",
}


def format_task_table(tasks: list[dict]) -> Table:
    table = Table(title="Tasks", show_header=True, header_style="bold")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Title", max_width=40)
    table.add_column("State", justify="center")
    table.add_column("Priority", justify="center")
    table.add_column("Type", style="dim")
    table.add_column("Updated", style="dim")

    for t in tasks:
        state = t.get("state", "?")
        priority = t.get("priority", "?")
        state_style = STATE_COLORS.get(state, "white")
        priority_style = PRIORITY_COLORS.get(priority, "white")

        table.add_row(
            t["id"][:8],
            t.get("title", "Untitled"),
            f"[{state_style}]{state}[/{state_style}]",
            f"[{priority_style}]{priority}[/{priority_style}]",
            t.get("task_type", "?"),
            t.get("updated_at", "")[:10],
        )

    return table


def format_task_detail(task: dict) -> Panel:
    lines = Text()
    state = task.get("state", "?")
    state_style = STATE_COLORS.get(state, "white")

    lines.append("ID: ", style="dim")
    lines.append(f"{task['id']}\n")
    lines.append("Title: ", style="dim")
    lines.append(f"{task.get('title', 'Untitled')}\n", style="bold white")

    if task.get("description"):
        lines.append("Description: ", style="dim")
        lines.append(f"{task['description']}\n")

    lines.append("State: ", style="dim")
    lines.append(f"{state}", style=state_style)
    if task.get("sub_state"):
        lines.append(f" ({task['sub_state']})", style="dim")
    lines.append("\n")

    priority = task.get("priority", "?")
    lines.append("Priority: ", style="dim")
    lines.append(f"{priority}\n", style=PRIORITY_COLORS.get(priority, "white"))

    lines.append("Type: ", style="dim")
    lines.append(f"{task.get('task_type', '?')}\n")

    if task.get("error_message"):
        lines.append("\nError: ", style="bold red")
        lines.append(f"{task['error_message']}\n", style="red")

    if task.get("result_summary"):
        lines.append("\nResult: ", style="dim")
        lines.append(f"{task['result_summary']}\n", style="green")

    if task.get("provider_name"):
        lines.append("Provider: ", style="dim")
        model = task.get("provider_model") or task.get("ai_model") or "default"
        lines.append(f"{task['provider_name']} ({model})\n")

    if task.get("actual_tokens"):
        lines.append("Tokens: ", style="dim")
        lines.append(f"{task['actual_tokens']:,}")
        if task.get("cost_usd"):
            lines.append(f" (${task['cost_usd']:.4f})")
        lines.append("\n")

    lines.append("Created: ", style="dim")
    lines.append(f"{task.get('created_at', '?')[:19]}\n")

    if task.get("completed_at"):
        lines.append("Completed: ", style="dim")
        lines.append(f"{task['completed_at'][:19]}\n")

    # Plan summary
    plan = task.get("plan_json")
    if plan and isinstance(plan, dict):
        lines.append("\n--- Plan ---\n", style="bold")
        if plan.get("summary"):
            lines.append(f"{plan['summary']}\n")
        sub_tasks_plan = plan.get("sub_tasks", [])
        if sub_tasks_plan:
            for i, st in enumerate(sub_tasks_plan):
                lines.append(
                    f"  {i + 1}. [{st.get('agent_role', '?')}] {st.get('title', '')}\n"
                )

    # Subtasks
    sub_tasks = task.get("sub_tasks", [])
    if sub_tasks:
        lines.append(f"\n--- Subtasks ({len(sub_tasks)}) ---\n", style="bold")
        completed = sum(
            1 for st in sub_tasks if st.get("status") == "completed"
        )
        lines.append(f"Progress: {completed}/{len(sub_tasks)} completed\n\n")

        for st in sub_tasks:
            status = st.get("status", "?")
            status_style = SUBTASK_STATUS_COLORS.get(status, "white")

            if status == "completed":
                icon = "v"
            elif status == "running":
                icon = ">"
            elif status == "failed":
                icon = "x"
            elif status == "cancelled":
                icon = "-"
            else:
                icon = "o"

            role = st.get("agent_role", "?")
            progress = st.get("progress_pct", 0)

            lines.append(f"  {icon} ", style=status_style)
            lines.append(f"{status:>10} ", style=status_style)
            lines.append(f"({role}) ", style="dim")
            lines.append(f"{st.get('title', 'Untitled')}")
            if status == "running" and progress > 0:
                lines.append(f" [{progress}%]", style="cyan")
            if st.get("error_message"):
                lines.append(
                    f" Error: {st['error_message'][:60]}", style="red"
                )
            lines.append("\n")

    # Action hints
    lines.append("\n")
    if state == "plan_ready":
        lines.append(
            "Actions: 'agents tasks approve <id>' or 'agents tasks reject <id>'",
            style="yellow",
        )
    elif state == "review":
        lines.append(
            "Actions: 'agents tasks accept <id>' to accept deliverables",
            style="yellow",
        )
    elif state in ("in_progress", "planning", "intake"):
        lines.append(
            "Actions: 'agents tasks cancel <id>' to cancel", style="yellow"
        )

    return Panel(
        lines,
        title=task.get("title", "Task"),
        border_style="cyan",
        padding=(1, 2),
    )
