"""Project-level chat: natural language interface for creating tasks,
asking questions about the project, debugging, etc.

Supports multi-session chat and plan mode.
LLM response generators are in project_chat_llm.py.
"""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access
from agents.api.routes.project_chat_llm import (
    create_tasks_from_plan,
    detect_intent,
    generate_create_task_response,
    generate_debug_response,
    generate_plan_response,
    generate_project_response,
)
from agents.utils.json_helpers import safe_json

logger = logging.getLogger(__name__)
router = APIRouter()


class ProjectChatInput(BaseModel):
    content: str
    intent: str | None = None  # 'create_task' | 'ask' | 'debug' | None (auto-detect)
    model: str | None = None  # Model override from chat header selector


class SessionCreateInput(BaseModel):
    mode: str = "auto"  # 'auto' | 'chat' | 'plan' — defaults to auto mode
    title: str | None = None


class SessionUpdateInput(BaseModel):
    title: str | None = None


# ── Session CRUD ──────────────────────────────────────────────────


@router.get("/projects/{project_id}/chat/sessions")
async def list_sessions(project_id: str, user: CurrentUser, db: DB):
    """List all chat sessions for a project (shared across members)."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        "SELECT s.*, u.display_name AS creator_name, u.avatar_url AS creator_avatar_url "
        "FROM project_chat_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.project_id = $1 ORDER BY s.updated_at DESC",
        project_id,
    )
    return [dict(r) for r in rows]


@router.post("/projects/{project_id}/chat/sessions")
async def create_session(
    project_id: str, body: SessionCreateInput, user: CurrentUser, db: DB
):
    """Create a new chat session."""
    await check_project_access(db, project_id, user)
    if body.mode not in ("auto", "chat", "plan"):
        raise HTTPException(status_code=400, detail="Mode must be 'auto', 'chat', or 'plan'")

    title = body.title or ("New Plan" if body.mode == "plan" else "New Chat")
    row = await db.fetchrow(
        """
        INSERT INTO project_chat_sessions (project_id, user_id, title, plan_mode, chat_mode)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        project_id,
        user["id"],
        title,
        body.mode == "plan",
        body.mode,
    )
    return dict(row)


@router.get("/projects/{project_id}/chat/sessions/{session_id}")
async def get_session(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Get a session with its messages (includes sender info)."""
    session = await _load_session(session_id, project_id, user, db)
    messages = await db.fetch(
        "SELECT m.*, u.display_name AS sender_name, u.avatar_url AS sender_avatar_url "
        "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = $1 ORDER BY m.created_at ASC",
        session_id,
    )
    return {**session, "messages": [dict(m) for m in messages]}


@router.put("/projects/{project_id}/chat/sessions/{session_id}")
async def update_session(
    project_id: str, session_id: str, body: SessionUpdateInput, user: CurrentUser, db: DB
):
    """Update session metadata."""
    await _load_session(session_id, project_id, user, db)
    if body.title:
        await db.execute(
            "UPDATE project_chat_sessions SET title = $2, updated_at = NOW() WHERE id = $1",
            session_id,
            body.title,
        )
    row = await db.fetchrow("SELECT * FROM project_chat_sessions WHERE id = $1", session_id)
    return dict(row)


@router.post("/projects/{project_id}/chat/sessions/{session_id}/toggle-plan")
async def toggle_plan_mode(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Toggle plan mode for a session."""
    session = await _load_session(session_id, project_id, user, db)
    new_val = not session.get("plan_mode", False)
    await db.execute(
        "UPDATE project_chat_sessions SET plan_mode = $2, updated_at = NOW() WHERE id = $1",
        session_id,
        new_val,
    )
    return {"plan_mode": new_val}


@router.post("/projects/{project_id}/chat/sessions/{session_id}/mode")
async def set_chat_mode(
    project_id: str, session_id: str, user: CurrentUser, db: DB,
    body: dict = None,
):
    """Set the chat mode for a session (chat, plan, debug, create_task)."""
    await _load_session(session_id, project_id, user, db)
    mode = (body or {}).get("mode", "chat")
    if mode not in ("auto", "chat", "plan", "debug", "create_task"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    await db.execute(
        "UPDATE project_chat_sessions SET chat_mode = $2, updated_at = NOW() WHERE id = $1",
        session_id, mode,
    )
    return {"mode": mode}


@router.delete("/projects/{project_id}/chat/sessions/{session_id}")
async def delete_session(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Delete a session and all its messages.

    Only the session creator or project owner can delete.
    Refuses deletion if the session is linked to a task (either via
    session.linked_todo_id or todo_items.chat_session_id).
    """
    user_role = await check_project_access(db, project_id, user)
    session = await _load_session(session_id, project_id, user, db)

    # Only creator or project owner can delete
    if str(session["user_id"]) != str(user["id"]) and user_role != "owner":
        raise HTTPException(status_code=403, detail="Only session creator or project owner can delete")

    # Check both directions of session ↔ task linking
    linked_todo_id = session.get("linked_todo_id")
    if not linked_todo_id:
        linked = await db.fetchrow(
            "SELECT id FROM todo_items WHERE chat_session_id = $1 LIMIT 1",
            session_id,
        )
        if linked:
            linked_todo_id = str(linked["id"])

    if linked_todo_id:
        todo = await db.fetchrow(
            "SELECT id, title FROM todo_items WHERE id = $1", linked_todo_id,
        )
        if todo:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"Cannot delete: this session is linked to task \"{todo['title']}\"",
                    "linked_task_id": str(todo["id"]),
                    "linked_task_title": todo["title"],
                },
            )

    await db.execute("DELETE FROM project_chat_messages WHERE session_id = $1", session_id)
    await db.execute("DELETE FROM project_chat_sessions WHERE id = $1", session_id)
    return {"status": "deleted"}


# ── Session Messages ──────────────────────────────────────────────


@router.post("/projects/{project_id}/chat/sessions/{session_id}/messages")
async def send_session_message(
    project_id: str,
    session_id: str,
    body: ProjectChatInput,
    user: CurrentUser,
    db: DB,
    event_bus: EventBusDep,
    redis: Redis,
):
    """Send a message in a session."""
    session = await _load_session(session_id, project_id, user, db)
    project = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Auto-title: use first user message as session title
    if dict(session).get("title") in ("New Chat", "New Plan"):
        title = body.content[:60].strip()
        if len(body.content) > 60:
            title += "..."
        await db.execute(
            "UPDATE project_chat_sessions SET title = $2, updated_at = NOW() WHERE id = $1",
            session_id,
            title,
        )

    # Store user message
    user_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'user', $3, $4, $5) RETURNING *
        """,
        project_id,
        user["id"],
        body.content,
        {"intent": body.intent} if body.intent else None,
        session_id,
    )

    placeholder_id = None
    try:
        model_override = body.model or dict(session).get("ai_model")

        if body.model:
            await db.execute(
                "UPDATE project_chat_sessions SET ai_model = $2, updated_at = NOW() WHERE id = $1",
                session_id, body.model,
            )

        chat_mode = dict(session).get("chat_mode", "auto")
        if chat_mode == "chat" and dict(session).get("plan_mode"):
            chat_mode = "plan"

        # Resolve routing mode
        routing_mode = chat_mode
        mode_auto_switched = False

        if chat_mode == "auto":
            last_mode = dict(session).get("last_routing_mode") or "chat"

            # Try keyword classification first (instant, no LLM call).
            # Only resolve provider + call LLM if keywords are ambiguous.
            from agents.api.routes.project_chat_llm import _keyword_classify
            kw_mode = _keyword_classify(body.content)
            if kw_mode:
                detected_mode, confidence = kw_mode, 1.0
                logger.info("intent: keyword → %s session=%s", kw_mode, session_id)
            elif len(body.content) < 40:
                detected_mode, confidence = last_mode, 0.8
                logger.info("intent: short follow-up, keeping %s session=%s", last_mode, session_id)
            else:
                from agents.providers.registry import get_registry
                registry = get_registry(db)
                provider = await registry.resolve_for_project(project_id, str(user["id"]))

                if redis:
                    await redis.publish(
                        f"chat:session:{session_id}:activity",
                        json.dumps({"type": "activity", "activity": "Routing..."}),
                    )

                recent_msgs = await db.fetch(
                    "SELECT role, content FROM project_chat_messages "
                    "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 3",
                    session_id,
                )
                detected_mode, confidence = await detect_intent(
                    provider=provider,
                    user_message=body.content,
                    current_routing_mode=last_mode,
                    recent_messages=[dict(m) for m in reversed(recent_msgs)],
                )
                logger.info(
                    "intent: LLM → %s conf=%.2f (last=%s) session=%s",
                    detected_mode, confidence, last_mode, session_id,
                )

            routing_mode = detected_mode
            if routing_mode != last_mode:
                mode_auto_switched = True

        # Insert placeholder assistant message so page refresh shows "Generating..."
        placeholder_msg = await db.fetchrow(
            """
            INSERT INTO project_chat_messages
              (project_id, user_id, role, content, metadata_json, session_id)
            VALUES ($1, $2, 'assistant', '', $3, $4) RETURNING *
            """,
            project_id, user["id"],
            json.dumps({"status": "generating"}),
            session_id,
        )
        placeholder_id = str(placeholder_msg["id"])

        # Dispatch to the resolved handler
        logger.info("dispatching to handler: mode=%s session=%s", routing_mode, session_id)
        kw: dict = dict(
            project_id=project_id,
            session_id=session_id,
            user_id=str(user["id"]),
            user_message=body.content,
            user_display_name=user.get("display_name", ""),
            project=dict(project),
            db=db,
            event_bus=event_bus,
            redis=redis,
            model_override=model_override,
            placeholder_id=placeholder_id,
        )
        if routing_mode == "plan":
            kw["session"] = dict(session)
            assistant_msg = await generate_plan_response(**kw)
        elif routing_mode == "debug":
            assistant_msg = await generate_debug_response(**kw)
        elif routing_mode == "create_task":
            assistant_msg = await generate_create_task_response(**kw)
        else:
            kw["intent"] = body.intent
            assistant_msg = await generate_project_response(**kw)

        # Update sticky routing mode
        if chat_mode == "auto":
            await db.execute(
                "UPDATE project_chat_sessions SET last_routing_mode = $2, updated_at = NOW() WHERE id = $1",
                session_id, routing_mode,
            )

        # Update session timestamp
        await db.execute(
            "UPDATE project_chat_sessions SET updated_at = NOW() WHERE id = $1", session_id
        )

        # Enrich messages with sender info
        user_msg_dict = dict(user_msg)
        user_msg_dict["sender_name"] = user.get("display_name", "")
        user_msg_dict["sender_avatar_url"] = user.get("avatar_url")

        assistant_msg_dict = dict(assistant_msg)
        # Assistant messages inherit the user_id of the requester but represent the AI
        assistant_msg_dict["sender_name"] = "AI"
        assistant_msg_dict["sender_avatar_url"] = None

        # Notify WebSocket subscribers that the assistant message is ready
        # (handles page-refresh mid-generation: the refreshed page will get this event)
        if redis and session_id:
            await redis.publish(
                f"chat:session:{session_id}:activity",
                json.dumps({
                    "type": "chat_message",
                    "message": {
                        "id": str(assistant_msg_dict["id"]),
                        "role": "assistant",
                        "content": assistant_msg_dict["content"],
                        "metadata_json": assistant_msg_dict.get("metadata_json"),
                        "sender_name": "AI",
                        "created_at": str(assistant_msg_dict.get("created_at", "")),
                    },
                }),
            )

        return {
            "user_message": user_msg_dict,
            "assistant_message": assistant_msg_dict,
            "routing_mode": routing_mode,
            "mode_auto_switched": mode_auto_switched,
        }
    except Exception as e:
        logger.error("Project chat error: %s", e)
        # Remove the placeholder so it doesn't linger as "generating"
        if placeholder_id:
            await db.execute(
                "DELETE FROM project_chat_messages WHERE id = $1",
                placeholder_id,
            )
        err_msg = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
            VALUES ($1, $2, 'system', $3, $4) RETURNING *
            """,
            project_id,
            user["id"],
            f"Error: {str(e)}",
            session_id,
        )
        user_msg_dict = dict(user_msg)
        user_msg_dict["sender_name"] = user.get("display_name", "")
        user_msg_dict["sender_avatar_url"] = user.get("avatar_url")
        return {
            "user_message": user_msg_dict,
            "assistant_message": dict(err_msg),
        }


@router.post("/projects/{project_id}/chat/sessions/{session_id}/accept-plan")
async def accept_session_plan(
    project_id: str, session_id: str, user: CurrentUser, db: DB, event_bus: EventBusDep,
):
    """Accept the proposed plan and create tasks directly, bypassing LLM."""
    session = await _load_session(session_id, project_id, user, db)
    plan_json = session.get("plan_json")
    if not plan_json:
        raise HTTPException(status_code=400, detail="No plan to accept")

    plan_json = safe_json(plan_json) if isinstance(plan_json, str) else plan_json

    created_tasks = await create_tasks_from_plan(
        project_id=project_id,
        user_id=str(user["id"]),
        plan=plan_json,
        db=db,
        event_bus=event_bus,
        session_id=session_id,
    )

    await db.execute(
        "UPDATE project_chat_sessions SET plan_mode = FALSE, updated_at = NOW() WHERE id = $1",
        session_id,
    )

    task_summary = "\n".join(f"  - {t['title']}" for t in plan_json.get("tasks", []))
    content = f"**Plan accepted!** Created {len(created_tasks)} tasks:\n{task_summary}"

    metadata = {
        "action": "plan_accepted",
        "plan_mode": False,
        "tasks_created": len(created_tasks),
        "task_ids": created_tasks,
    }

    # Store user "approve" message + assistant confirmation
    user_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
        VALUES ($1, $2, 'user', 'approve', $3) RETURNING *
        """,
        project_id, user["id"], session_id,
    )

    assistant_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
        """,
        project_id, user["id"], content, metadata, session_id,
    )

    return {
        "user_message": dict(user_msg),
        "assistant_message": dict(assistant_msg),
    }


@router.post("/projects/{project_id}/chat/sessions/{session_id}/accept-task-plan")
async def accept_task_plan(
    project_id: str, session_id: str, user: CurrentUser, db: DB, event_bus: EventBusDep,
):
    """Accept a task plan from create_task mode and create the task in DB."""
    session = await _load_session(session_id, project_id, user, db)
    task_plan = session.get("task_plan_json")
    if not task_plan:
        raise HTTPException(status_code=400, detail="No task plan to accept")

    task_plan = safe_json(task_plan) if isinstance(task_plan, str) else task_plan

    plan_json = task_plan.get("plan_json", {})
    intake_data = task_plan.get("intake_data", {})

    # Collect existing active subtasks from old linked todo (if any)
    existing_active_subtasks = []
    old_linked_todo_id = session.get("linked_todo_id")
    if old_linked_todo_id:
        rows = await db.fetch(
            """SELECT id, title, agent_role, status
               FROM sub_tasks
               WHERE todo_id = $1 AND status IN ('pending', 'assigned', 'running')
               ORDER BY execution_order""",
            old_linked_todo_id,
        )
        existing_active_subtasks = [
            {"id": str(r["id"]), "title": r["title"], "agent_role": r["agent_role"], "status": r["status"]}
            for r in rows
        ]

    # Create the task in DB
    todo = await db.fetchrow(
        """
        INSERT INTO todo_items (
            project_id, creator_id, title, description, priority, task_type,
            state, plan_json, intake_data, chat_session_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, 'plan_ready', $7, $8, $9)
        RETURNING *
        """,
        project_id,
        user["id"],
        task_plan.get("title", "Untitled"),
        task_plan.get("description", ""),
        task_plan.get("priority", "medium"),
        task_plan.get("task_type", "general"),
        plan_json,
        intake_data,
        session_id,
    )
    todo_id = str(todo["id"])

    # Link session and clear stored plan
    await db.execute(
        "UPDATE project_chat_sessions SET linked_todo_id = $1, task_plan_json = NULL, updated_at = NOW() WHERE id = $2",
        todo_id, session_id,
    )

    # Insert sub-tasks from plan_json and approve (transition to in_progress)
    from agents.utils.repo_utils import resolve_target_repo

    project_row = await db.fetchrow("SELECT context_docs FROM projects WHERE id = $1", project_id)
    context_docs = []
    if project_row and project_row.get("context_docs"):
        raw = project_row["context_docs"]
        context_docs = safe_json(raw) if isinstance(raw, str) else raw
        if not isinstance(context_docs, list):
            context_docs = []

    sub_task_ids = []
    sub_tasks = plan_json.get("sub_tasks", [])
    for st in sub_tasks:
        review_loop = bool(st.get("review_loop", False))
        target_repo = resolve_target_repo(st.get("target_repo"), context_docs)
        row = await db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, input_context, review_loop, target_repo
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            todo_id,
            st.get("title", ""),
            st.get("description", ""),
            st.get("agent_role", "coder"),
            st.get("execution_order", 0),
            st.get("context", {}),
            review_loop,
            target_repo,
        )
        sub_task_ids.append(str(row["id"]))

    # Set depends_on relationships
    for i, st in enumerate(sub_tasks):
        deps = st.get("depends_on", [])
        if deps:
            dep_ids = [sub_task_ids[j] for j in deps if j < len(sub_task_ids)]
            if dep_ids:
                await db.execute(
                    "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                    sub_task_ids[i], dep_ids,
                )

    # Transition to in_progress and emit event
    from agents.orchestrator.state_machine import transition_todo
    from agents.orchestrator.events import TaskEvent

    await transition_todo(db, todo_id, "in_progress", sub_state="executing", event_bus=event_bus)
    await event_bus.publish(TaskEvent(event_type="task_activated", todo_id=todo_id, state="in_progress"))

    # Store confirmation messages
    task_title = task_plan.get("title", "Untitled")
    content = f"**Task created and execution started:** {task_title}"
    metadata = {
        "action": "task_created",
        "task_id": todo_id,
        "task_title": task_title,
    }

    user_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
        VALUES ($1, $2, 'user', 'approve task plan', $3) RETURNING *
        """,
        project_id, user["id"], session_id,
    )
    assistant_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
        """,
        project_id, user["id"], content, metadata, session_id,
    )

    result = {
        "user_message": dict(user_msg),
        "assistant_message": dict(assistant_msg),
        "task_id": todo_id,
    }
    if existing_active_subtasks:
        result["existing_active_subtasks"] = existing_active_subtasks
        result["old_todo_id"] = str(old_linked_todo_id)
    return result


class CancelSubtasksInput(BaseModel):
    subtask_ids: list[str]


@router.post("/projects/{project_id}/chat/sessions/{session_id}/cancel-subtasks")
async def cancel_subtasks(
    project_id: str, session_id: str, body: CancelSubtasksInput,
    user: CurrentUser, db: DB,
):
    """Cancel selected subtasks on a session's linked task."""
    session = await _load_session(session_id, project_id, user, db)
    linked_todo_id = session.get("linked_todo_id")
    if not linked_todo_id:
        # Also check old linked todo via todo_items table
        linked = await db.fetchrow(
            "SELECT id FROM todo_items WHERE chat_session_id = $1 LIMIT 1",
            session_id,
        )
        if linked:
            linked_todo_id = str(linked["id"])
    if not linked_todo_id:
        raise HTTPException(status_code=400, detail="Session has no linked task")

    cancelled = []
    for st_id in body.subtask_ids:
        row = await db.fetchrow(
            "SELECT id, status FROM sub_tasks WHERE id = $1 AND todo_id = $2",
            st_id, linked_todo_id,
        )
        if row and row["status"] in ("pending", "assigned", "running"):
            await db.execute(
                "UPDATE sub_tasks SET status = 'cancelled' WHERE id = $1", st_id,
            )
            cancelled.append(st_id)

    return {"cancelled": cancelled, "todo_id": str(linked_todo_id)}


class DiscardTaskPlanInput(BaseModel):
    feedback: str = ""


@router.post("/projects/{project_id}/chat/sessions/{session_id}/discard-task-plan")
async def discard_task_plan(
    project_id: str, session_id: str, body: DiscardTaskPlanInput,
    user: CurrentUser, db: DB,
):
    """Discard the current task plan and allow re-planning with feedback."""
    session = await _load_session(session_id, project_id, user, db)
    if not session.get("task_plan_json"):
        raise HTTPException(status_code=400, detail="No task plan to discard")

    await db.execute(
        "UPDATE project_chat_sessions SET task_plan_json = NULL, updated_at = NOW() WHERE id = $1",
        session_id,
    )

    return {"status": "discarded"}


@router.delete("/projects/{project_id}/chat/sessions/{session_id}/messages/{message_id}")
async def delete_session_message(
    project_id: str, session_id: str, message_id: str, user: CurrentUser, db: DB
):
    """Delete a single message. If it created a task, also delete the task."""
    msg = await db.fetchrow(
        "SELECT * FROM project_chat_messages WHERE id = $1 AND session_id = $2",
        message_id,
        session_id,
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    # Any project member can remove messages from context
    await check_project_access(db, project_id, user)

    metadata = safe_json(msg.get("metadata_json"))
    task_id = metadata.get("task_id")
    if task_id:
        await db.execute("DELETE FROM todo_items WHERE id = $1", task_id)

    await db.execute("DELETE FROM project_chat_messages WHERE id = $1", message_id)
    return {"status": "deleted"}


class InjectInput(BaseModel):
    content: str


@router.post("/projects/{project_id}/chat/sessions/{session_id}/inject")
async def inject_session_message(
    project_id: str, session_id: str, body: InjectInput,
    user: CurrentUser, db: DB, redis: Redis,
):
    """Inject a user guidance message into a running chat session's tool loop."""
    await check_project_access(db, project_id, user)

    session = await db.fetchrow(
        "SELECT id FROM project_chat_sessions WHERE id = $1 AND project_id = $2",
        session_id, project_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await redis.rpush(f"chat:session:{session_id}:inject", body.content)
    await redis.expire(f"chat:session:{session_id}:inject", 3600)

    await redis.publish(
        f"chat:session:{session_id}:activity",
        json.dumps({"type": "user_inject", "content": body.content}),
    )

    return {"status": "queued"}


# ── Legacy endpoints (backward compat, use default session) ───────


@router.get("/projects/{project_id}/chat")
async def get_project_chat(project_id: str, user: CurrentUser, db: DB):
    """Get chat history for a project (legacy: returns all non-session messages + default session)."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        "SELECT * FROM project_chat_messages WHERE project_id = $1 AND user_id = $2 "
        "AND session_id IS NULL ORDER BY created_at ASC",
        project_id,
        user["id"],
    )
    return [dict(r) for r in rows]


@router.delete("/projects/{project_id}/chat")
async def clear_project_chat(project_id: str, user: CurrentUser, db: DB):
    """Clear all non-session chat messages for a project."""
    await check_project_access(db, project_id, user)
    await db.execute(
        "DELETE FROM project_chat_messages WHERE project_id = $1 AND user_id = $2 AND session_id IS NULL",
        project_id,
        user["id"],
    )
    return {"status": "cleared"}


@router.delete("/projects/{project_id}/chat/{message_id}")
async def delete_chat_message(project_id: str, message_id: str, user: CurrentUser, db: DB):
    """Delete a single chat message (legacy)."""
    msg = await db.fetchrow(
        "SELECT * FROM project_chat_messages WHERE id = $1 AND project_id = $2",
        message_id,
        project_id,
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(msg["user_id"]) != str(user["id"]) and user["role"] != "admin":
        raise HTTPException(status_code=403)

    metadata = safe_json(msg.get("metadata_json"))
    task_id = metadata.get("task_id")
    if task_id:
        await db.execute("DELETE FROM todo_items WHERE id = $1", task_id)

    await db.execute("DELETE FROM project_chat_messages WHERE id = $1", message_id)
    return {"status": "deleted"}


@router.post("/projects/{project_id}/chat")
async def send_project_chat(
    project_id: str, body: ProjectChatInput, user: CurrentUser, db: DB, event_bus: EventBusDep, redis: Redis,
):
    """Send a project chat message (legacy, no session)."""
    project = await _check_project_access_local(project_id, user, db)

    user_msg = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json)
        VALUES ($1, $2, 'user', $3, $4) RETURNING *
        """,
        project_id,
        user["id"],
        body.content,
        {"intent": body.intent} if body.intent else None,
    )

    try:
        assistant_msg = await generate_project_response(
            project_id=project_id,
            session_id=None,
            user_id=str(user["id"]),
            user_message=body.content,
            intent=body.intent,
            project=project,
            db=db,
            event_bus=event_bus,
            redis=redis,
        )
        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(assistant_msg),
        }
    except Exception as e:
        logger.error("Project chat error: %s", e)
        err_msg = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content)
            VALUES ($1, $2, 'system', $3) RETURNING *
            """,
            project_id,
            user["id"],
            f"Error: {str(e)}",
        )
        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(err_msg),
        }


# ── Internal Helpers ─────────────────────────────────────────────────


async def _check_project_access_local(project_id: str, user: dict, db) -> dict:
    """Verify access and return the project row."""
    await check_project_access(db, project_id, user)
    project = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(project)


async def _load_session(session_id: str, project_id: str, user: dict, db) -> dict:
    """Load a session — any project member can access (shared sessions)."""
    await check_project_access(db, project_id, user)
    session = await db.fetchrow(
        "SELECT * FROM project_chat_sessions WHERE id = $1 AND project_id = $2",
        session_id,
        project_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return dict(session)
