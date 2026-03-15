"""Rich formatters for chat messages."""

from rich.markdown import Markdown
from rich.panel import Panel


def format_user_message(content: str) -> Panel:
    return Panel(content, title="You", border_style="blue", padding=(0, 1))


def format_assistant_message(content: str) -> Panel:
    try:
        md = Markdown(content)
        return Panel(md, title="Assistant", border_style="green", padding=(0, 1))
    except Exception:
        return Panel(
            content, title="Assistant", border_style="green", padding=(0, 1)
        )
