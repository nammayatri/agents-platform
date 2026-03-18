"""Todo (task) CRUD and state transition endpoints.

Workspace IDE endpoints (file browsing, editing, git operations) are in
workspace.py to keep this module focused on task lifecycle management.
"""

import json
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access
from agents.config.settings import settings
from agents.orchestrator.events import TaskEvent
from agents.orchestrator.state_machine import transition_todo, transition_subtask
from agents.schemas.todo import (
    CreateTodoInput,
    RejectPlanInput,
    RequestChangesInput,
    UpdateTodoInput,
)
from agents.utils.git_utils import run_git_command


class RetryInput(BaseModel):
    with_context: bool = False


class TriggerSubTaskInput(BaseModel):
    force: bool = False


class InjectInput(BaseModel):
    content: str


router = APIRouter()


# ── CRUD ─────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/todos")
async def list_todos(
    project_id: str,
    user: CurrentUser,
    db: DB,
    state: str | None = None,
    priority: str | None = None,
):
    await check_project_access(db, project_id, user)

    query = "SELECT * FROM todo_items WHERE project_id = $1"
    params: list = [project_id]

    if state:
        params.append(state)
        query += f" AND state = ${len(params)}"
    if priority:
        params.append(priority)
        query += f" AND priority = ${len(params)}"

    query += " ORDER BY created_at DESC"
    rows = await db.fetch(query, *params)

    results = []
    for r in rows:
        item = dict(r)
        if item.get("ai_provider_id"):
            prov = await db.fetchrow(
                "SELECT display_name, default_model, provider_type FROM ai_provider_configs WHERE id = $1",
                item["ai_provider_id"],
            )
            if prov:
                item["provider_name"] = prov["display_name"]
                item["provider_model"] = item.get("ai_model") or prov["default_model"]
                item["provider_type"] = prov["provider_type"]
        results.append(item)
    return results


@router.post("/projects/{project_id}/todos", status_code=status.HTTP_201_CREATED)
async def create_todo(
    body: CreateTodoInput, project_id: str, user: CurrentUser, db: DB, event_bus: EventBusDep,
):
    await check_project_access(db, project_id, user)

    scheduled_at = None
    initial_state = "intake"
    if body.scheduled_at:
        try:
            scheduled_at = datetime.fromisoformat(body.scheduled_at)
            initial_state = "scheduled"
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format (use ISO 8601)")

    row = await db.fetchrow(
        """
        INSERT INTO todo_items (
            project_id, creator_id, title, description, priority, labels,
            task_type, ai_provider_id, ai_model, state, scheduled_at,
            rules_override_json, max_iterations
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
        RETURNING *
        """,
        project_id,
        user["id"],
        body.title,
        body.description,
        body.priority,
        body.labels,
        body.task_type,
        body.ai_provider_id,
        body.ai_model,
        initial_state,
        scheduled_at,
        json.dumps(body.rules_override_json) if body.rules_override_json else None,
        body.max_iterations,
    )

    if initial_state == "intake":
        await event_bus.publish(TaskEvent(
            event_type="task_created",
            todo_id=str(row["id"]),
            state="intake",
        ))

    return dict(row)


@router.get("/todos/{todo_id}")
async def get_todo(todo_id: str, user: CurrentUser, db: DB):
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    result = dict(todo)
    sub_tasks = await db.fetch(
        "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
        todo_id,
    )
    result["sub_tasks"] = [dict(s) for s in sub_tasks]

    if result.get("ai_provider_id"):
        prov = await db.fetchrow(
            "SELECT display_name, default_model, provider_type FROM ai_provider_configs WHERE id = $1",
            result["ai_provider_id"],
        )
        if prov:
            result["provider_name"] = prov["display_name"]
            result["provider_model"] = prov["default_model"]
            result["provider_type"] = prov["provider_type"]

    return result


ALLOWED_TODO_UPDATE_COLS = {"title", "description", "priority", "labels"}


@router.put("/todos/{todo_id}")
async def update_todo(todo_id: str, body: UpdateTodoInput, user: CurrentUser, db: DB):
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    updates = {k: v for k, v in body.model_dump().items() if v is not None and k in ALLOWED_TODO_UPDATE_COLS}
    if not updates:
        return dict(todo)

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i + 2}")
        values.append(v)

    updated = await db.fetchrow(
        f"UPDATE todo_items SET {', '.join(set_parts)}, updated_at = NOW() "
        f"WHERE id = $1 RETURNING *",
        todo_id,
        *values,
    )
    return dict(updated)


# ── State Transitions ────────────────────────────────────────────────


@router.post("/todos/{todo_id}/cancel")
async def cancel_todo(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    result = await transition_todo(db, todo_id, "cancelled", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Cannot cancel in current state")

    await db.execute(
        """
        UPDATE sub_tasks
        SET status = 'cancelled', updated_at = NOW()
        WHERE todo_id = $1 AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        todo_id,
    )

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "cancelled"}),
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "task_cancelled"}),
    )
    return result


@router.post("/todos/{todo_id}/retry")
async def retry_todo(
    todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
    body: RetryInput | None = None,
):
    if body is None:
        body = RetryInput()

    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo["state"] not in ("failed", "cancelled", "completed"):
        raise HTTPException(status_code=400, detail="Can only retry failed, cancelled, or completed tasks")

    if todo["state"] in ("completed", "failed"):
        needs_pr_only = await _check_needs_pr_only(db, todo_id)
        if needs_pr_only:
            return await _retry_pr_creation(db, redis, event_bus, todo_id, todo)

    prev_context = await _build_previous_run_context(db, todo_id, todo)
    intake = todo.get("intake_data") or {}
    if isinstance(intake, str):
        intake = json.loads(intake)
    intake["previous_run"] = prev_context
    await db.execute(
        "UPDATE todo_items SET intake_data = $2 WHERE id = $1",
        todo_id,
        intake,
    )

    result = await transition_todo(db, todo_id, "intake", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Transition failed")

    await db.execute(
        "UPDATE todo_items SET retry_count = 0, error_message = NULL, "
        "completed_at = NULL WHERE id = $1",
        todo_id,
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "intake"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="intake",
    ))
    return result


@router.post("/todos/{todo_id}/subtasks/{subtask_id}/retry")
async def retry_subtask(
    todo_id: str, subtask_id: str, user: CurrentUser, db: DB,
    redis: Redis, event_bus: EventBusDep,
):
    """Retry a specific failed sub-task."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    sub_task = await db.fetchrow(
        "SELECT * FROM sub_tasks WHERE id = $1 AND todo_id = $2",
        subtask_id, todo_id,
    )
    if not sub_task:
        raise HTTPException(status_code=404, detail="Sub-task not found")

    if sub_task["status"] not in ("failed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Can only retry failed or completed sub-tasks (current: {sub_task['status']})",
        )

    await db.execute(
        "UPDATE sub_tasks SET status = 'pending', error_message = NULL, "
        "updated_at = NOW() WHERE id = $1",
        subtask_id,
    )

    if todo["state"] in ("completed", "failed"):
        result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)
        if not result:
            await transition_todo(db, todo_id, "intake", event_bus=event_bus)
            result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)

        await db.execute(
            "UPDATE todo_items SET error_message = NULL, completed_at = NULL WHERE id = $1",
            todo_id,
        )

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))

    return {"status": "retrying", "subtask_id": subtask_id, "agent_role": sub_task["agent_role"]}


@router.post("/todos/{todo_id}/subtasks/{subtask_id}/inject")
async def inject_subtask_message(
    todo_id: str, subtask_id: str, body: InjectInput,
    user: CurrentUser, db: DB, redis: Redis,
):
    """Inject a user guidance message into a running subtask's tool loop."""
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    sub_task = await db.fetchrow(
        "SELECT status FROM sub_tasks WHERE id = $1 AND todo_id = $2",
        subtask_id, todo_id,
    )
    if not sub_task:
        raise HTTPException(status_code=404, detail="Sub-task not found")
    if sub_task["status"] != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Can only inject into running subtasks (current: {sub_task['status']})",
        )

    await redis.rpush(f"subtask:{subtask_id}:inject", body.content)
    await redis.expire(f"subtask:{subtask_id}:inject", 3600)

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({
            "type": "user_inject",
            "sub_task_id": subtask_id,
            "content": body.content,
        }),
    )

    return {"status": "queued", "subtask_id": subtask_id}


@router.post("/todos/{todo_id}/accept-deliverables")
async def accept_deliverables(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    result = await transition_todo(db, todo_id, "completed", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Cannot complete in current state")
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "completed"}),
    )
    return result


@router.post("/todos/{todo_id}/request-changes")
async def request_changes(
    todo_id: str, body: RequestChangesInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Cannot request changes in current state")

    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'user', $2)",
        todo_id,
        f"[Change Request]: {body.feedback}",
    )
    await redis.rpush(f"task:{todo_id}:chat_input", body.feedback)
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))
    return result


# ── Plan Approval ────────────────────────────────────────────────────


@router.post("/todos/{todo_id}/approve-plan")
async def approve_plan(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    """Approve a plan and begin execution."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo["state"] != "plan_ready":
        raise HTTPException(status_code=400, detail="Task is not awaiting plan approval")

    plan = todo.get("plan_json")
    if not plan:
        raise HTTPException(status_code=400, detail="No plan data found")

    if isinstance(plan, str):
        plan = json.loads(plan)

    # Load context_docs for target_repo resolution
    from agents.utils.repo_utils import resolve_target_repo
    project_row = await db.fetchrow(
        "SELECT context_docs FROM projects WHERE id = $1", todo["project_id"],
    )
    context_docs = []
    if project_row and project_row.get("context_docs"):
        raw = project_row["context_docs"]
        context_docs = json.loads(raw) if isinstance(raw, str) else raw

    sub_task_ids = []
    for st in plan.get("sub_tasks", []):
        review_loop = bool(st.get("review_loop", False))
        target_repo = resolve_target_repo(st.get("target_repo"), context_docs)
        row = await db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, input_context, review_loop, target_repo
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb)
            RETURNING id
            """,
            todo_id,
            st["title"],
            st.get("description", ""),
            st["agent_role"],
            st.get("execution_order", 0),
            json.dumps(st.get("context", {})),
            review_loop,
            json.dumps(target_repo) if target_repo else None,
        )
        sub_task_ids.append(str(row["id"]))

        if review_loop:
            await db.execute(
                "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                row["id"],
            )

    for i, st in enumerate(plan.get("sub_tasks", [])):
        depends_on = st.get("depends_on", [])
        if depends_on:
            dep_ids = [sub_task_ids[j] for j in depends_on if j < len(sub_task_ids)]
            if dep_ids:
                await db.execute(
                    "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                    sub_task_ids[i],
                    dep_ids,
                )

    result = await transition_todo(db, todo_id, "in_progress", sub_state="executing", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Transition failed")

    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'system', $2)",
        todo_id,
        "Plan approved. Starting execution.",
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))
    return result


@router.post("/todos/{todo_id}/reject-plan")
async def reject_plan(
    todo_id: str, body: RejectPlanInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    """Reject a plan and send it back for re-planning with feedback."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo["state"] != "plan_ready":
        raise HTTPException(status_code=400, detail="Task is not awaiting plan approval")

    await db.execute(
        "UPDATE todo_items SET plan_json = NULL, updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute("DELETE FROM sub_tasks WHERE todo_id = $1", todo_id)

    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'user', $2)",
        todo_id,
        f"[Plan Rejected]: {body.feedback}",
    )
    await redis.rpush(f"task:{todo_id}:chat_input", body.feedback)

    result = await transition_todo(db, todo_id, "planning", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Transition failed")

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "planning"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="planning",
    ))
    return result


# ── Merge Approval ───────────────────────────────────────────────────


@router.post("/todos/{todo_id}/approve-merge")
async def approve_merge(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    """Approve a pending merge."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_merge_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting merge approval")

    await db.execute(
        "UPDATE todo_items SET sub_state = 'merge_approved', updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'system', $2)",
        todo_id,
        "Merge approved by user. Proceeding with merge.",
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress", "sub_state": "merge_approved"}),
    )
    await event_bus.publish(TaskEvent(
        event_type="merge_approved",
        todo_id=todo_id,
        state="in_progress",
    ))
    return {"status": "approved"}


@router.post("/todos/{todo_id}/reject-merge")
async def reject_merge(
    todo_id: str, body: RequestChangesInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    """Reject a pending merge."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_merge_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting merge approval")

    merge_st = await db.fetchrow(
        "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'merge_agent' AND status = 'pending' LIMIT 1",
        todo_id,
    )
    if merge_st:
        await transition_subtask(
            db, str(merge_st["id"]), "failed",
            error_message=f"Merge rejected by user: {body.feedback}",
        )

    await db.execute(
        "UPDATE todo_items SET sub_state = 'executing', updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'user', $2)",
        todo_id,
        f"[Merge Rejected]: {body.feedback}",
    )
    await redis.rpush(f"task:{todo_id}:chat_input", body.feedback)
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress"}),
    )
    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))
    return {"status": "rejected"}


# ── Release Approval ────────────────────────────────────────────────


@router.post("/todos/{todo_id}/approve-release")
async def approve_release(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    """Approve a pending production release."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_release_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting release approval")

    await db.execute(
        "UPDATE todo_items SET sub_state = 'release_prod_approved', updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'system', $2)",
        todo_id,
        "Production release approved by user. Proceeding with deployment.",
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress", "sub_state": "release_prod_approved"}),
    )
    await event_bus.publish(TaskEvent(
        event_type="release_approved",
        todo_id=todo_id,
        state="in_progress",
    ))
    return {"status": "approved"}


@router.post("/todos/{todo_id}/reject-release")
async def reject_release(
    todo_id: str, body: RequestChangesInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    """Reject a pending production release."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_release_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting release approval")

    release_st = await db.fetchrow(
        "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'release_deployer' "
        "AND status = 'pending' AND description LIKE '%prod%' LIMIT 1",
        todo_id,
    )
    if release_st:
        await transition_subtask(
            db, str(release_st["id"]), "failed",
            error_message=f"Production release rejected by user: {body.feedback}",
        )

    await db.execute(
        "UPDATE todo_items SET sub_state = 'release_rejected', updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'user', $2)",
        todo_id,
        f"[Release Rejected]: {body.feedback}",
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress", "sub_state": "release_rejected"}),
    )
    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))
    return {"status": "rejected"}


# ── Subtask Trigger ──────────────────────────────────────────────────


@router.post("/todos/{todo_id}/sub-tasks/{sub_task_id}/trigger")
async def trigger_subtask(
    todo_id: str,
    sub_task_id: str,
    body: TriggerSubTaskInput,
    user: CurrentUser,
    db: DB,
    redis: Redis,
    event_bus: EventBusDep,
):
    """Manually trigger a blocked or failed subtask."""
    todo = await db.fetchrow("SELECT project_id, state FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail="Task not found")
    await check_project_access(db, str(todo["project_id"]), user)

    st = await db.fetchrow(
        "SELECT id, status, depends_on FROM sub_tasks WHERE id = $1 AND todo_id = $2",
        sub_task_id, todo_id,
    )
    if not st:
        raise HTTPException(status_code=404, detail="Subtask not found")

    if st["status"] in ("running", "assigned"):
        raise HTTPException(status_code=400, detail="Subtask is already running")

    if body.force:
        await db.execute(
            "UPDATE sub_tasks SET status = 'pending', depends_on = '{}', "
            "error_message = NULL, updated_at = NOW() WHERE id = $1",
            sub_task_id,
        )
    else:
        await db.execute(
            "UPDATE sub_tasks SET status = 'pending', "
            "error_message = NULL, updated_at = NOW() WHERE id = $1",
            sub_task_id,
        )

    if todo["state"] not in ("in_progress",):
        await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)

    await event_bus.publish(TaskEvent(
        event_type="state_changed",
        todo_id=todo_id,
        state="in_progress",
    ))

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "activity", "activity": f"Subtask manually triggered: {sub_task_id[:8]}"}),
    )

    return {"status": "triggered", "sub_task_id": sub_task_id, "force": body.force}


# ── Internal Helpers ─────────────────────────────────────────────────


async def _check_needs_pr_only(db, todo_id: str) -> bool:
    """Check if the task has completed coder work but no PR deliverable."""
    coder_count = await db.fetchval(
        "SELECT COUNT(*) FROM sub_tasks WHERE todo_id = $1 "
        "AND agent_role = 'coder' AND status = 'completed'",
        todo_id,
    )
    if not coder_count:
        return False

    pr_exists = await db.fetchrow(
        "SELECT id FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
        todo_id,
    )
    if pr_exists:
        return False

    unfinished = await db.fetchval(
        "SELECT COUNT(*) FROM sub_tasks WHERE todo_id = $1 "
        "AND agent_role NOT IN ('pr_creator', 'merge_agent') "
        "AND status IN ('pending', 'failed', 'running', 'assigned')",
        todo_id,
    )
    if unfinished:
        return False

    last_reviewer = await db.fetchrow(
        "SELECT review_verdict FROM sub_tasks WHERE todo_id = $1 "
        "AND agent_role = 'reviewer' AND status = 'completed' "
        "ORDER BY created_at DESC LIMIT 1",
        todo_id,
    )
    if last_reviewer and last_reviewer["review_verdict"] == "needs_changes":
        return False

    return True


async def _retry_pr_creation(db, redis, event_bus, todo_id: str, todo: dict) -> dict:
    """Retry just the PR creation step."""
    existing_pr_task = await db.fetchrow(
        "SELECT id, status FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
        "ORDER BY created_at DESC LIMIT 1",
        todo_id,
    )

    if existing_pr_task and existing_pr_task["status"] in ("failed", "completed"):
        await db.execute(
            "UPDATE sub_tasks SET status = 'pending', error_message = NULL, "
            "updated_at = NOW() WHERE id = $1",
            existing_pr_task["id"],
        )
    elif not existing_pr_task:
        coder_subtasks = await db.fetch(
            "SELECT id, target_repo FROM sub_tasks WHERE todo_id = $1 "
            "AND agent_role = 'coder' AND status = 'completed' ORDER BY created_at DESC",
            todo_id,
        )
        all_coder_ids = [str(st["id"]) for st in coder_subtasks]
        target_repo_json = coder_subtasks[0].get("target_repo") if coder_subtasks else None
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        max_order = await db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            todo_id,
        )
        await db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6)
            RETURNING id
            """,
            todo_id,
            "Create Pull Request",
            "Commit all workspace changes, push to a feature branch, and create a pull request.",
            max_order + 1,
            all_coder_ids,
            target_repo_json,
        )

    result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)
    if not result:
        await transition_todo(db, todo_id, "intake", event_bus=event_bus)
        result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Could not transition task for PR retry")

    await db.execute(
        "UPDATE todo_items SET error_message = NULL, completed_at = NULL, "
        "sub_state = 'pr_retry' WHERE id = $1",
        todo_id,
    )

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "in_progress"}),
    )

    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="in_progress",
    ))
    return result


async def _build_previous_run_context(db, todo_id: str, todo) -> dict:
    """Gather summary from the previous execution for retry context."""
    sub_tasks = await db.fetch(
        "SELECT title, agent_role, status, output_result, error_message "
        "FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order",
        todo_id,
    )

    ctx: dict = {
        "result_summary": todo.get("result_summary") or "",
        "previous_state": todo["state"],
        "sub_tasks": [
            {
                "title": st["title"],
                "role": st["agent_role"],
                "status": st["status"],
                "output": (
                    json.loads(st["output_result"])
                    if isinstance(st["output_result"], str)
                    else st["output_result"]
                ) if st["output_result"] else None,
                "error": st["error_message"],
            }
            for st in sub_tasks
        ],
    }

    git_diff = await _get_task_workspace_diff(str(todo["project_id"]), todo_id)
    if git_diff:
        ctx["git_diff"] = git_diff

    # Collect diffs from dependency workspaces
    task_dir = os.path.join(
        settings.workspace_root, str(todo["project_id"]), "tasks", todo_id,
    )
    if os.path.isdir(task_dir):
        dep_diffs = {}
        for entry in os.listdir(task_dir):
            if entry.startswith("dep_") and os.path.isdir(os.path.join(task_dir, entry)):
                dep_name = entry[4:]  # strip "dep_" prefix
                dep_diff = await _get_task_workspace_diff_for_dir(
                    os.path.join(task_dir, entry, "repo")
                )
                if dep_diff:
                    dep_diffs[dep_name] = dep_diff
        if dep_diffs:
            ctx["dep_diffs"] = dep_diffs

    return ctx


async def _get_task_workspace_diff(project_id: str, todo_id: str) -> dict | None:
    """Get git diff from the main task workspace for retry context."""
    repo_dir = os.path.join(
        settings.workspace_root, project_id, "tasks", todo_id, "repo",
    )
    return await _get_task_workspace_diff_for_dir(repo_dir)


async def _get_task_workspace_diff_for_dir(repo_dir: str) -> dict | None:
    """Get git diff from any repo directory for retry context."""
    if not os.path.isdir(repo_dir):
        return None

    try:
        rc, base_branch = await run_git_command(
            "rev-parse", "--abbrev-ref", "HEAD@{upstream}", cwd=repo_dir,
        )
        if rc != 0:
            for fallback in ("origin/main", "origin/master"):
                rc2, _ = await run_git_command("rev-parse", "--verify", fallback, cwd=repo_dir)
                if rc2 == 0:
                    base_branch = fallback
                    break
            else:
                base_branch = "HEAD~1"
        else:
            base_branch = base_branch.strip()

        rc, stat_output = await run_git_command("diff", "--stat", base_branch, cwd=repo_dir)
        if rc != 0 or not stat_output.strip():
            return None

        _, names_output = await run_git_command("diff", "--name-status", base_branch, cwd=repo_dir)
        files = []
        for line in names_output.strip().split("\n"):
            if line.strip():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    files.append({"status": parts[0], "path": parts[1]})

        _, diff_output = await run_git_command("diff", base_branch, cwd=repo_dir)
        max_diff = 15000
        truncated = len(diff_output) > max_diff
        diff_text = diff_output[:max_diff]
        if truncated:
            diff_text += "\n... (diff truncated)"

        return {
            "stat": stat_output.strip(),
            "files": files,
            "diff": diff_text,
            "truncated": truncated,
        }
    except Exception:
        return None
