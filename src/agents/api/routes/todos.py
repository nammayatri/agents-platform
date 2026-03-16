import asyncio
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
                item["provider_model"] = item.get("ai_model") or prov["default_model"]
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

    # Cancel all pending/assigned/running subtasks
    await db.execute(
        """
        UPDATE sub_tasks
        SET status = 'cancelled', updated_at = NOW()
        WHERE todo_id = $1 AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        todo_id,
    )

    # Publish cancellation events so the coordinator stops
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

    # Check if this is a completed/failed task that just needs a PR
    # (has completed coder work but no pull_request deliverable)
    if todo["state"] in ("completed", "failed"):
        needs_pr_only = await _check_needs_pr_only(db, todo_id)
        if needs_pr_only:
            return await _retry_pr_creation(db, redis, event_bus, todo_id, todo)

    # Full retry: gather context from previous run and restart from intake
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


async def _check_needs_pr_only(db, todo_id: str) -> bool:
    """Check if the task has completed coder work but no PR deliverable."""
    # Has completed coder subtasks?
    coder_count = await db.fetchval(
        "SELECT COUNT(*) FROM sub_tasks WHERE todo_id = $1 "
        "AND agent_role = 'coder' AND status = 'completed'",
        todo_id,
    )
    if not coder_count:
        return False

    # Already has a PR?
    pr_exists = await db.fetchrow(
        "SELECT id FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
        todo_id,
    )
    if pr_exists:
        return False

    # Has a failed or no pr_creator subtask?
    pr_task = await db.fetchrow(
        "SELECT id, status FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
        "ORDER BY created_at DESC LIMIT 1",
        todo_id,
    )
    # Either no pr_creator exists, or it failed — needs PR retry
    return True


async def _retry_pr_creation(db, redis, event_bus, todo_id: str, todo: dict) -> dict:
    """Retry just the PR creation step instead of full task retry."""
    # Reset any failed pr_creator subtask, or create a new one
    existing_pr_task = await db.fetchrow(
        "SELECT id, status FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
        "ORDER BY created_at DESC LIMIT 1",
        todo_id,
    )

    if existing_pr_task and existing_pr_task["status"] in ("failed", "completed"):
        # Reset to pending for retry
        await db.execute(
            "UPDATE sub_tasks SET status = 'pending', error_message = NULL, "
            "updated_at = NOW() WHERE id = $1",
            existing_pr_task["id"],
        )
    elif not existing_pr_task:
        # Create a new pr_creator subtask
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

    # Transition to in_progress so orchestrator picks it up
    result = await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)
    if not result:
        # Fallback: try intake → in_progress
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


@router.post("/todos/{todo_id}/subtasks/{subtask_id}/retry")
async def retry_subtask(
    todo_id: str, subtask_id: str, user: CurrentUser, db: DB,
    redis: Redis, event_bus: EventBusDep,
):
    """Retry a specific failed sub-task (e.g., pr_creator, merge_agent)."""
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

    # Reset the sub-task to pending
    await db.execute(
        "UPDATE sub_tasks SET status = 'pending', error_message = NULL, "
        "updated_at = NOW() WHERE id = $1",
        subtask_id,
    )

    # If the parent todo is completed or failed, transition to in_progress
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


async def _build_previous_run_context(db, todo_id: str, todo) -> dict:
    """Gather summary from the previous execution to feed into next run.

    Includes subtask results and the git diff from the task workspace
    so the planner knows what code changes already exist.
    """
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

    # Capture git diff from the task workspace
    git_diff = await _get_task_workspace_diff(str(todo["project_id"]), todo_id)
    if git_diff:
        ctx["git_diff"] = git_diff

    return ctx


async def _get_task_workspace_diff(project_id: str, todo_id: str) -> dict | None:
    """Get git diff from the task workspace, comparing against the base branch."""
    repo_dir = os.path.join(
        settings.workspace_root, project_id, "tasks", todo_id, "repo",
    )
    if not os.path.isdir(repo_dir):
        return None

    async def _run_git(*args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode(errors="replace")

    try:
        # Find the merge-base with the default branch to get the full diff
        # of changes made by the agent (not just the last commit)
        rc, base_branch = await _run_git(
            "rev-parse", "--abbrev-ref", "HEAD@{upstream}",
        )
        if rc != 0:
            # Fallback: try origin/main or origin/master
            for fallback in ("origin/main", "origin/master"):
                rc2, _ = await _run_git("rev-parse", "--verify", fallback)
                if rc2 == 0:
                    base_branch = fallback
                    break
            else:
                # No upstream found — diff against HEAD~1 as last resort
                base_branch = "HEAD~1"
        else:
            base_branch = base_branch.strip()

        # Diff stat (summary of changed files)
        rc, stat_output = await _run_git("diff", "--stat", base_branch)
        if rc != 0 or not stat_output.strip():
            return None

        # Changed files with status
        _, names_output = await _run_git("diff", "--name-status", base_branch)
        files = []
        for line in names_output.strip().split("\n"):
            if line.strip():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    files.append({"status": parts[0], "path": parts[1]})

        # Full diff (truncated to avoid blowing up context)
        _, diff_output = await _run_git("diff", base_branch)
        max_diff = 15000  # ~15k chars to stay within token budget
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


@router.post("/todos/{todo_id}/approve-merge")
async def approve_merge(todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep):
    """Approve a pending merge. Wakes the coordinator to proceed with merge."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_merge_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting merge approval")

    # Set sub_state to merge_approved so coordinator can proceed
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
    """Reject a pending merge. Fails the merge subtask and posts feedback."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    if todo.get("sub_state") != "awaiting_merge_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting merge approval")

    # Fail the pending merge subtask
    merge_st = await db.fetchrow(
        "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'merge_agent' AND status = 'pending' LIMIT 1",
        todo_id,
    )
    if merge_st:
        await transition_subtask(
            db, str(merge_st["id"]), "failed",
            error_message=f"Merge rejected by user: {body.feedback}",
        )

    # Clear sub_state
    await db.execute(
        "UPDATE todo_items SET sub_state = 'executing', updated_at = NOW() WHERE id = $1",
        todo_id,
    )
    # Post feedback as chat message
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


class TriggerSubTaskInput(BaseModel):
    force: bool = False  # If true, clears depends_on to force execution


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
    """Manually trigger a blocked or failed subtask.

    Resets the subtask to 'pending' so the coordinator picks it up.
    If force=true, also clears depends_on to bypass dependency checks.
    """
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

    # Reset subtask to pending
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

    # Ensure the parent todo is in a state the coordinator will pick up
    if todo["state"] not in ("in_progress",):
        await transition_todo(db, todo_id, "in_progress", event_bus=event_bus)

    # Publish event so coordinator re-evaluates
    await event_bus.publish(TaskEvent(
        event_type="state_changed",
        todo_id=todo_id,
        state="in_progress",
    ))

    # Notify via WebSocket
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "activity", "activity": f"Subtask manually triggered: {sub_task_id[:8]}"}),
    )

    return {"status": "triggered", "sub_task_id": sub_task_id, "force": body.force}
