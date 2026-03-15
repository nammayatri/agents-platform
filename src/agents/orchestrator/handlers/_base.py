"""HandlerContext — dependency-inverted context passed to all event handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    import asyncpg
    import redis.asyncio as aioredis
    from agents.orchestrator.workspace import WorkspaceManager


@dataclass
class HandlerContext:
    """Everything a handler needs, built once per dispatch from the coordinator."""

    todo_id: str
    db: asyncpg.Pool
    redis: aioredis.Redis
    workspace_mgr: WorkspaceManager

    # Bound callbacks from coordinator
    transition_subtask: Callable[..., Coroutine]   # (st_id, status, **kw)
    post_system_message: Callable[..., Coroutine]  # (content, metadata=None)
    report_progress: Callable[..., Coroutine]      # (st_id, pct, msg)
    report_activity: Callable[..., Coroutine]      # (st_id, activity)
    load_todo: Callable[..., Coroutine]            # () -> dict
    notifier: Any
