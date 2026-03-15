"""Rich formatters for project data."""

from rich.panel import Panel
from rich.text import Text


def format_project_detail(project: dict) -> Panel:
    lines = Text()
    lines.append("ID: ", style="dim")
    lines.append(f"{project['id']}\n")
    lines.append("Name: ", style="dim")
    lines.append(f"{project['name']}\n", style="bold white")

    if project.get("description"):
        lines.append("Description: ", style="dim")
        lines.append(f"{project['description']}\n")

    if project.get("repo_url"):
        lines.append("Repository: ", style="dim")
        lines.append(f"{project['repo_url']}\n", style="cyan")

    lines.append("Branch: ", style="dim")
    lines.append(f"{project.get('default_branch', 'main')}\n")

    if project.get("user_role"):
        role = project["user_role"]
        style = "green" if role == "owner" else "blue"
        lines.append("Your role: ", style="dim")
        lines.append(f"{role}\n", style=style)

    lines.append("Created: ", style="dim")
    lines.append(f"{project.get('created_at', '?')[:10]}\n")
    lines.append("Updated: ", style="dim")
    lines.append(f"{project.get('updated_at', '?')[:10]}\n")

    settings = project.get("settings_json") or {}
    understanding = settings.get("project_understanding", {})
    if understanding.get("tech_stack"):
        lines.append("\nTech stack: ", style="dim")
        lines.append(
            f"{', '.join(understanding['tech_stack'])}\n", style="magenta"
        )
    if understanding.get("summary"):
        lines.append(f"\n{understanding['summary']}\n", style="italic")

    return Panel(
        lines, title=project["name"], border_style="cyan", padding=(1, 2)
    )
