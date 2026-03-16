"""Project listing and detail commands."""

import click
from rich.console import Console
from rich.table import Table

from agents_cli import client
from agents_cli.display.projects import format_project_detail

console = Console()


@click.command("projects")
@click.argument("project_id", required=False)
@client.handle_errors
def projects_cmd(project_id: str | None):
    """List all projects, or show detail for a specific project."""
    client.require_auth()

    if project_id:
        project_id = client.resolve_project_id(project_id)
        project = client.get(f"/projects/{project_id}")
        console.print(format_project_detail(project))
    else:
        _list_projects()


def _list_projects():
    projects = client.get("/projects")

    if not projects:
        console.print("[dim]No projects found.[/dim]")
        return

    table = Table(title="Projects", show_header=True, header_style="bold")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Name", style="bold white")
    table.add_column("Role", style="cyan")
    table.add_column("Repo", style="dim")
    table.add_column("Updated", style="dim")

    for p in projects:
        role = p.get("user_role", "?")
        role_style = "green" if role == "owner" else "blue"
        repo = p.get("repo_url", "")
        repo_short = repo.split("/")[-1] if repo else "-"

        table.add_row(
            p["id"][:8],
            p["name"],
            f"[{role_style}]{role}[/{role_style}]",
            repo_short,
            p.get("updated_at", "")[:10],
        )

    console.print(table)
    console.print(
        "\n[dim]Use 'agents projects <id>' for details. Short ID prefixes work.[/dim]"
    )
