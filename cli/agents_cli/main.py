"""CLI entry point."""

import click

from agents_cli.commands import auth, projects, tasks, chat, config_cmd


@click.group()
@click.version_option(package_name="agents-cli")
def cli():
    """Agent Platform CLI -- manage projects, tasks, and chat from the terminal."""
    pass


cli.add_command(auth.login_cmd, "login")
cli.add_command(auth.logout_cmd, "logout")
cli.add_command(projects.projects_cmd, "projects")
cli.add_command(tasks.tasks_cmd, "tasks")
cli.add_command(chat.chat_cmd, "chat")
cli.add_command(config_cmd.config_group, "config")


if __name__ == "__main__":
    cli()
