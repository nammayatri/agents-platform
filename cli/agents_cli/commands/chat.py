"""Interactive project chat session -- launches Textual TUI."""

import click
from rich.console import Console

from agents_cli import client

console = Console()


@click.command("chat")
@click.argument("project_id")
@click.option(
    "--session",
    "-s",
    "session_id",
    default=None,
    help="Resume an existing session ID",
)
@click.option(
    "--plan/--no-plan",
    "plan_mode",
    default=True,
    help="Start in plan mode (default) or chat mode",
)
@client.handle_errors
def chat_cmd(project_id: str, session_id: str | None, plan_mode: bool):
    """Start an interactive chat session with a project."""
    client.require_auth()

    # Resolve short ID prefix to full UUID
    project_id = client.resolve_project_id(project_id)

    # Fetch project
    project = client.get(f"/projects/{project_id}")
    project_name = project["name"]

    # Create or load session
    messages = []
    if session_id:
        session = client.get(
            f"/projects/{project_id}/chat/sessions/{session_id}"
        )
        messages = session.get("messages", [])
        plan_mode = session.get("plan_mode", plan_mode)
    else:
        mode = "plan" if plan_mode else "chat"
        session = client.post(
            f"/projects/{project_id}/chat/sessions", {"mode": mode}
        )
        session_id = session["id"]
        plan_mode = session.get("plan_mode", plan_mode)

    # Launch the TUI
    from agents_cli.tui.app import ChatApp

    app = ChatApp(
        project_id=project_id,
        project_name=project_name,
        session_id=session_id,
        plan_mode=plan_mode,
        messages=messages,
    )
    app.run()
