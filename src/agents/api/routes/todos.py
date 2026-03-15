import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access
from agents.orchestrator.events import TaskEvent
from agents.orchestrator.state_machine import transition_todo
from agents.schemas.todo import (
    CreateTodoInput,
    RejectPlanInput,
    RequestChangesInput,
    UpdateTodoInput,
)


class RetryInput(BaseModel):
    with_context: bool = False

router = APIRouter()


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

    # Enrich with provider info
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
                item["provider_model"] = prov["default_model"]
                item["provider_type"] = prov["provider_type"]
        results.append(item)
    return results


@router.post("/projects/{project_id}/todos", status_code=status.HTTP_201_CREATED)
async def create_todo(
    body: CreateTodoInput, project_id: str, user: CurrentUser, db: DB, event_bus: EventBusDep,
):
    await check_project_access(db, project_id, user)

    # Determine initial state: scheduled or immediate
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
            task_type, ai_provider_id, state, scheduled_at,
            rules_override_json, max_iterations
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
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
        initial_state,
        scheduled_at,
        json.dumps(body.rules_override_json) if body.rules_override_json else None,
        body.max_iterations,
    )

    # Emit event for immediate tasks (scheduled tasks wait for their time)
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
    # Include sub-tasks
    sub_tasks = await db.fetch(
        "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
        todo_id,
    )
    result["sub_tasks"] = [dict(s) for s in sub_tasks]

    # Include provider info
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


@router.post("/todos/{todo_id}/cancel")
async def cancel_todo(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    result = await transition_todo(db, todo_id, "cancelled", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Cannot cancel in current state")
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "cancelled"}),
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

    # Optionally gather context from previous run
    if body.with_context:
        prev_context = await _build_previous_run_context(db, todo_id, todo)
        intake = todo.get("intake_data") or {}
        if isinstance(intake, str):
            intake = json.loads(intake)
        intake["previous_run"] = prev_context
        await db.execute(
            "UPDATE todo_items SET intake_data = $2::jsonb WHERE id = $1",
            todo_id,
            json.dumps(intake),
        )

    result = await transition_todo(db, todo_id, "intake", event_bus=event_bus)
    if not result:
        raise HTTPException(status_code=400, detail="Transition failed")

    # Reset retry count and clear error/completion state
    await db.execute(
        "UPDATE todo_items SET retry_count = 0, error_message = NULL, "
        "completed_at = NULL WHERE id = $1",
        todo_id,
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "state_change", "state": "intake"}),
    )

    # Emit event so orchestrator picks it up immediately
    await event_bus.publish(TaskEvent(
        event_type="task_activated",
        todo_id=todo_id,
        state="intake",
    ))
    return result


async def _build_previous_run_context(db, todo_id: str, todo) -> dict:
    """Gather summary from the previous execution to feed into next run."""
    sub_tasks = await db.fetch(
        "SELECT title, agent_role, status, output_result, error_message "
        "FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order",
        todo_id,
    )
    return {
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

    # Post feedback as chat message for the coordinator to pick up
    await db.execute(
        """
        INSERT INTO chat_messages (todo_id, role, content)
        VALUES ($1, 'user', $2)
        """,
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

    # Create sub-tasks from the approved plan
    sub_task_ids = []
    for st in plan.get("sub_tasks", []):
        row = await db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, input_context
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            todo_id,
            st["title"],
            st.get("description", ""),
            st["agent_role"],
            st.get("execution_order", 0),
            json.dumps(st.get("context", {})),
        )
        sub_task_ids.append(str(row["id"]))

    # Set up dependencies
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

    # Clear plan and any existing sub-tasks
    await db.execute(
        "UPDATE todo_items SET plan_json = NULL, updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    await db.execute("DELETE FROM sub_tasks WHERE todo_id = $1", todo_id)

    # Post user feedback as chat message
    await db.execute(
        "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'user', $2)",
        todo_id,
        f"[Plan Rejected]: {body.feedback}",
    )
    await redis.rpush(f"task:{todo_id}:chat_input", body.feedback)

    # Transition back to planning
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
