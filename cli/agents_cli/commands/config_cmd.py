"""Config management commands."""

import click
from rich.console import Console
from rich.table import Table

from agents_cli import config

console = Console()


@click.group("config", invoke_without_command=True)
@click.pass_context
def config_group(ctx):
    """Show or modify CLI configuration."""
    if ctx.invoked_subcommand is None:
        _show_config()


def _show_config():
    cfg = config.load()
    table = Table(title="CLI Configuration", show_header=True)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("API URL", cfg.get("api_url", "not set"))
    table.add_row(
        "Logged in as", cfg.get("user_email") or "[dim]not logged in[/dim]"
    )
    table.add_row(
        "Display name", cfg.get("user_display_name") or "[dim]-[/dim]"
    )
    table.add_row(
        "Token",
        "[green]present[/green]" if cfg.get("token") else "[red]none[/red]",
    )
    table.add_row("Config file", str(config.CONFIG_FILE))

    console.print(table)


@config_group.command("set-url")
@click.argument("url")
def set_url(url: str):
    """Set the API server URL."""
    config.set_api_url(url)
    console.print(f"[green]API URL set to: {url}[/green]")
