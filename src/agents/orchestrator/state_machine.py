"""Task state machine: validation, transitions, sub-task state management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from agents.orchestrator.events import EventBus

logger = logging.getLogger(__name__)

# Valid state transitions for TODO items
VALID_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"intake", "cancelled"},
    "intake": {"planning", "failed", "cancelled"},
    "planning": {"plan_ready", "in_progress", "failed", "cancelled"},
    "plan_ready": {"in_progress", "planning", "failed", "cancelled"},
    "in_progress": {"testing", "review", "failed", "planning", "cancelled"},
    "testing": {"review", "in_progress", "failed", "cancelled"},
    "review": {"completed", "in_progress", "failed", "cancelled"},
    "failed": {"intake", "in_progress"},  # in_progress for subtask-level retry (e.g. PR creation)
    "cancelled": {"intake"},
    "completed": {"intake", "in_progress"},  # in_progress for subtask-level retry (e.g. PR creation)
}

# Valid sub-task transitions
VALID_SUBTASK_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"assigned", "cancelled", "failed"},
    "assigned": {"running", "cancelled"},
    "running": {"completed", "failed", "cancelled", "pending"},  # pending = pause for retry (CI wait, merge approval)
    "failed": {"pending"},  # retry
}


def validate_transition(current: str, target: str) -> bool:
    return target in VALID_TRANSITIONS.get(current, set())


def validate_subtask_transition(current: str, target: str) -> bool:
    return target in VALID_SUBTASK_TRANSITIONS.get(current, set())


async def transition_todo(
    db: asyncpg.Pool,
    todo_id: str,
    target_state: str,
    *,
    sub_state: str | None = None,
    error_message: str | None = None,
    result_summary: str | None = None,
    event_bus: EventBus | None = None,
    redis=None,
) -> dict | None:
    """Atomically transition a TODO item to a new state with validation.

    If event_bus is provided, publishes a state_changed event after a successful
    transition so the event-driven orchestrator can react immediately.
    If redis is provided, publishes a state_change event to the WebSocket
    pub/sub channel for real-time UI updates.
    """
    row = await db.fetchrow("SELECT state FROM todo_items WHERE id = $1", todo_id)
    if not row:
        return None

    current = row["state"]
    if not validate_transition(current, target_state):
        raise ValueError(f"Invalid transition: {current} -> {target_state}")

    is_terminal = target_state in ("completed", "cancelled")
    completed_at = datetime.now(timezone.utc) if is_terminal else None

    updated = await db.fetchrow(
        """
        UPDATE todo_items
        SET state = $2, sub_state = $3, state_changed_at = NOW(),
            error_message = COALESCE($4, error_message),
            result_summary = COALESCE($5, result_summary),
            completed_at = COALESCE($6, completed_at),
            updated_at = NOW()
        WHERE id = $1 AND state = $7
        RETURNING *
        """,
        todo_id,
        target_state,
        sub_state,
        error_message,
        result_summary,
        completed_at,
        current,  # optimistic lock: only update if state hasn't changed
    )

    if updated and event_bus:
        from agents.orchestrator.events import TaskEvent

        try:
            await event_bus.publish(TaskEvent(
                event_type="state_changed",
                todo_id=todo_id,
                state=target_state,
                sub_state=sub_state,
                metadata={"previous_state": current},
            ))
        except Exception:
            logger.warning("Failed to publish state_changed event for %s", todo_id[:8], exc_info=True)

    # Publish to WebSocket channel for real-time UI updates
    if updated and redis:
        try:
            event: dict = {"type": "state_change", "state": target_state}
            if error_message:
                event["error_message"] = error_message
            await redis.publish(
                f"task:{todo_id}:events",
                json.dumps(event),
            )
        except Exception:
            logger.debug("Failed to publish WS state_change for %s", todo_id[:8])

    return dict(updated) if updated else None


async def transition_subtask(
    db: asyncpg.Pool,
    subtask_id: str,
    target_status: str,
    *,
    progress_pct: int | None = None,
    progress_message: str | None = None,
    output_result: dict | None = None,
    error_message: str | None = None,
    redis=None,
) -> dict | None:
    """Transition a sub-task to a new status.

    If redis is provided, publishes a state_change event to the parent
    todo's WebSocket channel so the UI refreshes subtask state.
    """
    row = await db.fetchrow("SELECT status FROM sub_tasks WHERE id = $1", subtask_id)
    if not row:
        return None

    current = row["status"]
    if not validate_subtask_transition(current, target_status):
        raise ValueError(f"Invalid sub-task transition: {current} -> {target_status}")

    completed_at = datetime.now(timezone.utc) if target_status == "completed" else None

    updated = await db.fetchrow(
        """
        UPDATE sub_tasks
        SET status = $2,
            progress_pct = COALESCE($3, progress_pct),
            progress_message = COALESCE($4, progress_message),
            output_result = COALESCE($5::jsonb, output_result),
            error_message = COALESCE($6, error_message),
            completed_at = COALESCE($7, completed_at),
            updated_at = NOW()
        WHERE id = $1 AND status = $8
        RETURNING *
        """,
        subtask_id,
        target_status,
        progress_pct,
        progress_message,
        json.dumps(output_result) if output_result else None,
        error_message,
        completed_at,
        current,
    )

    # Publish to WebSocket channel for real-time UI updates
    if updated and redis:
        todo_id = str(updated.get("todo_id", ""))
        if todo_id:
            try:
                event: dict = {
                    "type": "subtask_update",
                    "sub_task_id": subtask_id,
                    "status": target_status,
                }
                if error_message:
                    event["error_message"] = error_message
                await redis.publish(
                    f"task:{todo_id}:events",
                    json.dumps(event),
                )
            except Exception:
                logger.debug("Failed to publish WS subtask_update for %s", subtask_id[:8])

    return dict(updated) if updated else None


async def check_all_subtasks_done(db: asyncpg.Pool, todo_id: str) -> tuple[bool, bool]:
    """Check if all sub-tasks for a TODO are done.

    Returns (all_done, any_failed).
    """
    rows = await db.fetch(
        "SELECT status FROM sub_tasks WHERE todo_id = $1",
        todo_id,
    )
    if not rows:
        return True, False

    statuses = [r["status"] for r in rows]
    all_done = all(s in ("completed", "failed", "cancelled") for s in statuses)
    any_failed = any(s == "failed" for s in statuses)
    return all_done, any_failed
