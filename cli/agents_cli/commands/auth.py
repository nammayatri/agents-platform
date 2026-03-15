"""Login/logout commands."""

import click
from rich.console import Console

from agents_cli import client, config

console = Console()


@click.command("login")
@client.handle_errors
def login_cmd():
    """Log in with email and password."""
    existing_token = config.get_token()
    if existing_token:
        try:
            me = client.get_me()
            console.print(
                f"Already logged in as [bold]{me['display_name']}[/bold] ({me['email']})"
            )
            if not click.confirm("Log in as a different user?"):
                return
        except Exception:
            pass

    email = click.prompt("Email")
    password = click.prompt("Password", hide_input=True)

    result = client.login(email, password)
    token = result["access_token"]
    config.set_token(token, email=email)

    try:
        me = client.get_me()
        config.set_token(token, email=me["email"], display_name=me["display_name"])
        console.print(
            f"\n[green]Logged in as [bold]{me['display_name']}[/bold] ({me['email']})[/green]"
        )
    except Exception:
        console.print(f"\n[green]Logged in as {email}[/green]")


@click.command("logout")
def logout_cmd():
    """Clear stored credentials."""
    cfg = config.load()
    if not cfg.get("token"):
        console.print("[yellow]Not currently logged in.[/yellow]")
        return

    email = cfg.get("user_email", "unknown")
    config.clear_token()
    console.print(f"[green]Logged out ({email}).[/green]")
