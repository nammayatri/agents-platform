"""Task listing, detail, and action commands."""

import click
from rich.console import Console

from agents_cli import client
from agents_cli.display.tasks import format_task_table, format_task_detail

console = Console()


@click.group("tasks", invoke_without_command=True)
@click.argument("project_id", required=False)
@click.pass_context
def tasks_cmd(ctx, project_id: str | None):
    """List tasks for a project, or use subcommands for task actions."""
    if ctx.invoked_subcommand is None:
        if not project_id:
            console.print("[red]Usage: agents tasks <project-id>[/red]")
            raise SystemExit(1)
        client.require_auth()
        _list_tasks(project_id)


@client.handle_errors
def _list_tasks(project_id: str):
    project_id = client.resolve_project_id(project_id)
    tasks = client.get(f"/projects/{project_id}/todos")
    if not tasks:
        console.print("[dim]No tasks found for this project.[/dim]")
        return
    console.print(format_task_table(tasks))
    console.print(
        "\n[dim]Use 'agents tasks show <id>' for details. Short ID prefixes work.[/dim]"
    )


@tasks_cmd.command("show")
@click.argument("task_id")
@client.handle_errors
def show_task(task_id: str):
    """Show detailed task info with subtasks."""
    client.require_auth()
    task_id = client.resolve_todo_id(task_id)
    task = client.get(f"/todos/{task_id}")
    console.print(format_task_detail(task))


@tasks_cmd.command("approve")
@click.argument("task_id")
@client.handle_errors
def approve_plan(task_id: str):
    """Approve a task plan and start execution."""
    client.require_auth()
    task_id = client.resolve_todo_id(task_id)

    task = client.get(f"/todos/{task_id}")
    if task["state"] != "plan_ready":
        console.print(
            f"[red]Task is in state '{task['state']}', not 'plan_ready'. Cannot approve.[/red]"
        )
        raise SystemExit(1)

    # Show plan summary
    plan = task.get("plan_json")
    if plan and isinstance(plan, dict):
        sub_tasks = plan.get("sub_tasks", [])
        console.print(
            f"\n[bold]Plan Summary:[/bold] {plan.get('summary', 'N/A')}"
        )
        console.print(f"[bold]Subtasks:[/bold] {len(sub_tasks)}")
        for i, st in enumerate(sub_tasks):
            console.print(
                f"  {i + 1}. [{st.get('agent_role', '?')}] {st.get('title', '')}"
            )
        console.print()

    if not click.confirm("Approve this plan and start execution?"):
        return

    result = client.post(f"/todos/{task_id}/approve-plan")
    console.print(
        f"[green]Plan approved. Task is now '{result['state']}'.[/green]"
    )


@tasks_cmd.command("reject")
@click.argument("task_id")
@click.option(
    "--feedback",
    "-f",
    prompt="Rejection feedback",
    help="Feedback for why the plan is rejected",
)
@client.handle_errors
def reject_plan(task_id: str, feedback: str):
    """Reject a task plan and send it back for re-planning."""
    client.require_auth()
    task_id = client.resolve_todo_id(task_id)

    task = client.get(f"/todos/{task_id}")
    if task["state"] != "plan_ready":
        console.print(
            f"[red]Task is in state '{task['state']}', not 'plan_ready'. Cannot reject.[/red]"
        )
        raise SystemExit(1)

    result = client.post(
        f"/todos/{task_id}/reject-plan", {"feedback": feedback}
    )
    console.print(
        f"[yellow]Plan rejected. Task sent back to '{result['state']}'.[/yellow]"
    )


@tasks_cmd.command("cancel")
@click.argument("task_id")
@client.handle_errors
def cancel_task(task_id: str):
    """Cancel a task."""
    client.require_auth()
    task_id = client.resolve_todo_id(task_id)

    task = client.get(f"/todos/{task_id}")
    console.print(f"Task: [bold]{task['title']}[/bold] [{task['state']}]")

    if not click.confirm("Cancel this task?"):
        return

    client.post(f"/todos/{task_id}/cancel")
    console.print("[red]Task cancelled.[/red]")


@tasks_cmd.command("accept")
@click.argument("task_id")
@client.handle_errors
def accept_deliverables(task_id: str):
    """Accept task deliverables and mark as completed."""
    client.require_auth()
    task_id = client.resolve_todo_id(task_id)

    task = client.get(f"/todos/{task_id}")
    if task["state"] != "review":
        console.print(
            f"[red]Task is in state '{task['state']}', not 'review'. Cannot accept.[/red]"
        )
        raise SystemExit(1)

    if task.get("result_summary"):
        console.print(
            f"\n[bold]Result Summary:[/bold]\n{task['result_summary']}\n"
        )

    if not click.confirm(
        "Accept deliverables and mark task as completed?"
    ):
        return

    result = client.post(f"/todos/{task_id}/accept-deliverables")
    console.print(
        f"[green]Deliverables accepted. Task is now '{result['state']}'.[/green]"
    )
