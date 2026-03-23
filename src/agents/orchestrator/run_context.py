"""RunContext — stateless service bag replacing coordinator back-references.

Every agent and the scheduler receive a RunContext. It provides all
infrastructure services (db, redis, workspace, LLM, notifications) plus
helper methods for common operations (chat messages, progress reporting,
state transitions).

No circular dependencies: RunContext holds references to services,
not to the scheduler or any agent.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import asyncpg
import redis.asyncio as aioredis

from agents.infra.notifier import Notifier
from agents.orchestrator.state_machine import (
    transition_subtask as _transition_subtask,
    transition_todo as _transition_todo,
)
from agents.orchestrator.workspace import WorkspaceManager
from agents.providers.mcp_executor import McpToolExecutor
from agents.providers.registry import ProviderRegistry
from agents.providers.tools_registry import ToolsRegistry
from agents.schemas.agent import LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    """All services an agent or scheduler needs. No back-references."""

    todo_id: str
    db: asyncpg.Pool
    redis: aioredis.Redis
    workspace_mgr: WorkspaceManager
    provider_registry: ProviderRegistry
    mcp_executor: McpToolExecutor
    tools_registry: ToolsRegistry
    notifier: Notifier

    # Chat routing (resolved once at scheduler start)
    chat_session_id: str | None = None
    chat_project_id: str | None = None
    chat_user_id: str | None = None

    # Activity throttle state
    _last_activity_publish: dict[str, float] = field(default_factory=dict)
    _last_activity_persist: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def transition_todo(self, target_state: str, **kwargs) -> dict | None:
        result = await _transition_todo(
            self.db, self.todo_id, target_state, redis=self.redis, **kwargs,
        )
        if result is None:
            logger.warning(
                "[%s] transition_todo to '%s' failed (concurrent change)",
                self.todo_id, target_state,
            )
        return result

    async def transition_subtask(self, subtask_id: str, target_status: str, **kwargs) -> dict | None:
        result = await _transition_subtask(
            self.db, subtask_id, target_status, redis=self.redis, **kwargs,
        )
        if result is None:
            logger.warning(
                "[%s] transition_subtask %s→%s failed",
                self.todo_id, str(subtask_id)[:8], target_status,
            )
        return result

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    async def load_todo(self) -> dict:
        row = await self.db.fetchrow(
            "SELECT * FROM todo_items WHERE id = $1", self.todo_id,
        )
        return dict(row)

    async def load_project(self, project_id: str) -> dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", project_id,
        )
        return dict(row) if row else None

    async def is_cancelled(self) -> bool:
        state = await self.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", self.todo_id,
        )
        return state == "cancelled"

    # ------------------------------------------------------------------
    # Chat messaging
    # ------------------------------------------------------------------

    async def post_system_message(self, content: str, metadata: dict | None = None) -> None:
        await self._post_chat_message("system", content, metadata=metadata)

    async def post_assistant_message(self, content: str, metadata: dict | None = None) -> None:
        await self._post_chat_message("assistant", content, metadata=metadata)

    async def _post_chat_message(self, role: str, content: str, *, metadata: dict | None = None) -> None:
        if self.chat_session_id:
            if metadata:
                await self.db.execute(
                    "INSERT INTO project_chat_messages "
                    "(project_id, user_id, role, content, metadata_json, session_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    self.chat_project_id, self.chat_user_id, role, content,
                    metadata, self.chat_session_id,
                )
            else:
                await self.db.execute(
                    "INSERT INTO project_chat_messages "
                    "(project_id, user_id, role, content, session_id) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    self.chat_project_id, self.chat_user_id, role, content,
                    self.chat_session_id,
                )
        else:
            if metadata:
                await self.db.execute(
                    "INSERT INTO chat_messages (todo_id, role, content, metadata_json) "
                    "VALUES ($1, $2, $3, $4)",
                    self.todo_id, role, content, metadata,
                )
            else:
                await self.db.execute(
                    "INSERT INTO chat_messages (todo_id, role, content) "
                    "VALUES ($1, $2, $3)",
                    self.todo_id, role, content,
                )

        msg_payload = {"role": role, "content": content}
        if metadata:
            msg_payload["metadata_json"] = metadata
        event_data = json.dumps({"type": "chat_message", "message": msg_payload})
        await self.redis.publish(f"task:{self.todo_id}:events", event_data)
        if self.chat_session_id:
            await self.redis.publish(
                f"chat:session:{self.chat_session_id}:activity", event_data,
            )

    # ------------------------------------------------------------------
    # Progress / activity reporting
    # ------------------------------------------------------------------

    async def report_progress(self, subtask_id: str, pct: int, message: str) -> None:
        await self.db.execute(
            "UPDATE sub_tasks SET progress_pct = $2, progress_message = $3 WHERE id = $1",
            subtask_id, pct, message,
        )
        await self.redis.publish(
            f"task:{self.todo_id}:progress",
            json.dumps({
                "type": "progress",
                "sub_task_id": subtask_id,
                "progress_pct": pct,
                "message": message,
            }),
        )

    async def report_activity(self, subtask_id: str, activity: str) -> None:
        """Throttled activity update (Redis: 1/s, DB: 1/3s)."""
        now = time.monotonic()

        last_persist = self._last_activity_persist.get(subtask_id, 0)
        if now - last_persist >= 3.0:
            await self.db.execute(
                "UPDATE sub_tasks SET progress_message = $2 WHERE id = $1",
                subtask_id, activity,
            )
            self._last_activity_persist[subtask_id] = now

        last_publish = self._last_activity_publish.get(subtask_id, 0)
        if now - last_publish >= 1.0:
            await self.redis.publish(
                f"task:{self.todo_id}:progress",
                json.dumps({
                    "type": "activity",
                    "sub_task_id": subtask_id,
                    "activity": activity,
                }),
            )
            self._last_activity_publish[subtask_id] = now

    async def report_planning_activity(self, activity: str) -> None:
        """Throttled planning-phase activity (1/s)."""
        now = time.monotonic()
        last = self._last_activity_publish.get("_planning", 0)
        if now - last < 1.0:
            return
        self._last_activity_publish["_planning"] = now

        event_data = json.dumps({
            "type": "activity",
            "phase": "planning",
            "activity": activity,
        })
        await self.redis.publish(f"task:{self.todo_id}:progress", event_data)
        if self.chat_session_id:
            await self.redis.publish(
                f"chat:session:{self.chat_session_id}:activity", event_data,
            )

    def build_token_streamer(self, subtask_id: str | None = None):
        """Build a buffered on_token callback for streaming LLM text deltas."""
        channel = f"task:{self.todo_id}:progress"
        session_channel = (
            f"chat:session:{self.chat_session_id}:activity"
            if self.chat_session_id else None
        )
        _buf: list[str] = []
        _buf_len = 0
        redis_ref = self.redis

        async def on_token(delta: str) -> None:
            nonlocal _buf_len
            _buf.append(delta)
            _buf_len += len(delta)
            if _buf_len >= 20:
                text = "".join(_buf)
                _buf.clear()
                _buf_len = 0
                payload = json.dumps({
                    "type": "token",
                    "token": text,
                    **({"sub_task_id": subtask_id} if subtask_id else {}),
                })
                await redis_ref.publish(channel, payload)
                if session_channel:
                    await redis_ref.publish(session_channel, payload)

        async def flush() -> None:
            nonlocal _buf_len
            if _buf:
                text = "".join(_buf)
                _buf.clear()
                _buf_len = 0
                payload = json.dumps({
                    "type": "token",
                    "token": text,
                    **({"sub_task_id": subtask_id} if subtask_id else {}),
                })
                await redis_ref.publish(channel, payload)
                if session_channel:
                    await redis_ref.publish(session_channel, payload)

        on_token.flush = flush  # type: ignore[attr-defined]
        return on_token

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    async def track_tokens(self, response: LLMResponse) -> None:
        await self.db.execute(
            "UPDATE todo_items SET actual_tokens = actual_tokens + $2, "
            "cost_usd = cost_usd + $3, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            response.tokens_input + response.tokens_output,
            response.cost_usd,
        )

    # ------------------------------------------------------------------
    # User messages
    # ------------------------------------------------------------------

    async def check_for_user_messages(self) -> str | None:
        return await self.redis.lpop(f"task:{self.todo_id}:chat_input")
