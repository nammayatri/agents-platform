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
    mode: str = "plan"  # 'chat' | 'plan' — defaults to plan mode
    title: str | None = None


class SessionUpdateInput(BaseModel):
    title: str | None = None


# ── Session CRUD ──────────────────────────────────────────────────


@router.get("/projects/{project_id}/chat/sessions")
async def list_sessions(project_id: str, user: CurrentUser, db: DB):
    """List all chat sessions for a project."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        "SELECT * FROM project_chat_sessions WHERE project_id = $1 AND user_id = $2 "
        "ORDER BY updated_at DESC",
        project_id,
        user["id"],
    )
    return [dict(r) for r in rows]


@router.post("/projects/{project_id}/chat/sessions")
async def create_session(
    project_id: str, body: SessionCreateInput, user: CurrentUser, db: DB
):
    """Create a new chat session."""
    await check_project_access(db, project_id, user)
    if body.mode not in ("chat", "plan"):
        raise HTTPException(status_code=400, detail="Mode must be 'chat' or 'plan'")

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
    """Get a session with its messages."""
    session = await _load_session(session_id, project_id, user, db)
    messages = await db.fetch(
        "SELECT * FROM project_chat_messages WHERE session_id = $1 ORDER BY created_at ASC",
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
    if mode not in ("chat", "plan", "debug", "create_task"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    await db.execute(
        "UPDATE project_chat_sessions SET chat_mode = $2, updated_at = NOW() WHERE id = $1",
        session_id, mode,
    )
    return {"mode": mode}


@router.delete("/projects/{project_id}/chat/sessions/{session_id}")
async def delete_session(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Delete a session and all its messages."""
    await _load_session(session_id, project_id, user, db)
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
        VALUES ($1, $2, 'user', $3, $4::jsonb, $5) RETURNING *
        """,
        project_id,
        user["id"],
        body.content,
        json.dumps({"intent": body.intent}) if body.intent else None,
        session_id,
    )

    try:
        model_override = body.model or dict(session).get("ai_model")

        if body.model:
            await db.execute(
                "UPDATE project_chat_sessions SET ai_model = $2, updated_at = NOW() WHERE id = $1",
                session_id, body.model,
            )

        chat_mode = dict(session).get("chat_mode", "chat")
        if chat_mode == "chat" and dict(session).get("plan_mode"):
            chat_mode = "plan"

        if chat_mode == "plan":
            assistant_msg = await generate_plan_response(
                project_id=project_id,
                session_id=session_id,
                user_id=str(user["id"]),
                user_message=body.content,
                project=dict(project),
                session=dict(session),
                db=db,
                event_bus=event_bus,
                redis=redis,
                model_override=model_override,
            )
        elif chat_mode == "debug":
            assistant_msg = await generate_debug_response(
                project_id=project_id,
                session_id=session_id,
                user_id=str(user["id"]),
                user_message=body.content,
                project=dict(project),
                db=db,
                event_bus=event_bus,
                redis=redis,
                model_override=model_override,
            )
        elif chat_mode == "create_task":
            assistant_msg = await generate_create_task_response(
                project_id=project_id,
                session_id=session_id,
                user_id=str(user["id"]),
                user_message=body.content,
                project=dict(project),
                db=db,
                event_bus=event_bus,
                redis=redis,
                model_override=model_override,
            )
        else:
            assistant_msg = await generate_project_response(
                project_id=project_id,
                session_id=session_id,
                user_id=str(user["id"]),
                user_message=body.content,
                intent=body.intent,
                project=dict(project),
                db=db,
                event_bus=event_bus,
                redis=redis,
                model_override=model_override,
            )

        # Update session timestamp
        await db.execute(
            "UPDATE project_chat_sessions SET updated_at = NOW() WHERE id = $1", session_id
        )

        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(assistant_msg),
        }
    except Exception as e:
        logger.error("Project chat error: %s", e)
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
        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(err_msg),
        }


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
    if str(msg["user_id"]) != str(user["id"]) and user["role"] != "admin":
        raise HTTPException(status_code=403)

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
        "SELECT id FROM project_chat_sessions WHERE id = $1 AND project_id = $2 AND user_id = $3",
        session_id, project_id, user["id"],
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
        VALUES ($1, $2, 'user', $3, $4::jsonb) RETURNING *
        """,
        project_id,
        user["id"],
        body.content,
        json.dumps({"intent": body.intent}) if body.intent else None,
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
    session = await db.fetchrow(
        "SELECT * FROM project_chat_sessions WHERE id = $1 AND project_id = $2",
        session_id,
        project_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(session["user_id"]) != str(user["id"]) and user["role"] != "admin":
        raise HTTPException(status_code=403)
    return dict(session)
