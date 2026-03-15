import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException

from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access

logger = logging.getLogger(__name__)
from agents.orchestrator.events import TaskEvent
from agents.schemas.chat import ChatMessageInput

router = APIRouter()


@router.get("/todos/{todo_id}/chat")
async def get_chat_history(todo_id: str, user: CurrentUser, db: DB):
    todo = await db.fetchrow(
        "SELECT project_id, chat_session_id FROM todo_items WHERE id = $1", todo_id,
    )
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    session_id = todo.get("chat_session_id")
    if session_id:
        # Linked session: read from project_chat_messages
        rows = await db.fetch(
            """
            SELECT id, role, content, NULL::uuid as agent_run_id, created_at,
                   metadata_json
            FROM project_chat_messages WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            str(session_id),
        )
        # Map to chat_messages-compatible format
        return [
            {
                "id": str(r["id"]),
                "todo_id": todo_id,
                "role": r["role"],
                "content": r["content"],
                "agent_run_id": None,
                "metadata_json": r.get("metadata_json"),
                "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            }
            for r in rows
        ]

    rows = await db.fetch(
        "SELECT * FROM chat_messages WHERE todo_id = $1 ORDER BY created_at ASC",
        todo_id,
    )
    return [dict(r) for r in rows]


@router.post("/todos/{todo_id}/chat")
async def send_chat_message(
    todo_id: str, body: ChatMessageInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    todo = await db.fetchrow(
        "SELECT state, sub_state, project_id, creator_id, chat_session_id FROM todo_items WHERE id = $1",
        todo_id,
    )
    if not todo:
        raise HTTPException(status_code=404)

    session_id = todo.get("chat_session_id")
    project_id = str(todo["project_id"])

    if session_id:
        # Linked session: write to project_chat_messages
        msg = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
            VALUES ($1, $2, 'user', $3, $4) RETURNING *
            """,
            project_id,
            user["id"],
            body.content,
            str(session_id),
        )
        msg_id = str(msg["id"])
    else:
        # Standard: write to chat_messages
        msg = await db.fetchrow(
            """
            INSERT INTO chat_messages (todo_id, role, content)
            VALUES ($1, 'user', $2) RETURNING *
            """,
            todo_id,
            body.content,
        )
        msg_id = str(msg["id"])

    # Push to Redis for the coordinator to pick up (for steering during execution)
    await redis.rpush(f"task:{todo_id}:chat_input", body.content)

    # If the task was waiting for the user's response, clear the sub_state
    # and emit an event so the orchestrator picks it up immediately.
    if todo.get("sub_state") == "awaiting_response":
        await db.execute(
            "UPDATE todo_items SET sub_state = NULL, updated_at = NOW() WHERE id = $1",
            todo_id,
        )
        await event_bus.publish(TaskEvent(
            event_type="user_replied",
            todo_id=todo_id,
            state=todo["state"],
        ))

    # Publish event for WebSocket subscribers
    chat_event = json.dumps({
        "type": "chat_message",
        "message": {"role": "user", "content": body.content, "id": msg_id},
    })
    await redis.publish(f"task:{todo_id}:events", chat_event)
    if session_id:
        await redis.publish(f"chat:session:{session_id}:activity", chat_event)

    # Always generate a direct LLM response for the user -- don't make them
    # wait for the orchestrator poll cycle.  The orchestrator handles state
    # transitions; chat should always be fast and direct.
    asyncio.create_task(
        _direct_chat_response(todo_id, body.content, db, redis, event_bus, session_id=str(session_id) if session_id else None)
    )

    # Return in chat_messages-compatible format
    return {
        "id": msg_id,
        "todo_id": todo_id,
        "role": "user",
        "content": body.content,
        "created_at": msg["created_at"].isoformat() if hasattr(msg["created_at"], "isoformat") else str(msg["created_at"]),
    }


async def _direct_chat_response(
    todo_id: str, message: str, db, redis, event_bus=None, *, session_id: str | None = None,
):
    """Generate an immediate AI response to any user chat message."""
    from agents.api.chat_actions import execute_action, get_actions_as_tools, is_action_tool
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry

    try:
        registry = get_registry(db)
        provider = await registry.resolve_for_todo(todo_id)

        todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)

        if session_id:
            # Linked session: read history from project_chat_messages
            chat_history = await db.fetch(
                "SELECT role, content FROM project_chat_messages "
                "WHERE session_id = $1 ORDER BY created_at ASC",
                session_id,
            )
        else:
            chat_history = await db.fetch(
                "SELECT role, content FROM chat_messages WHERE todo_id = $1 ORDER BY created_at ASC",
                todo_id,
            )

        from agents.schemas.agent import LLMMessage

        # Resolve MCP tools for this project
        tools_reg = ToolsRegistry(db)
        mcp_tools = await tools_reg.resolve_tools(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )
        skills_ctx = await tools_reg.build_skills_context(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )

        # Build context-aware system prompt based on current state
        state = todo["state"]
        sub_state = todo.get("sub_state") or ""

        if state == "intake":
            system_context = (
                "This task is in the intake phase. The user may be providing additional context. "
                "Be brief — acknowledge what they said in one line. "
                "Do NOT ask follow-up questions unless the user explicitly asks you to clarify something. "
                "Default to confirming you have enough info to proceed."
            )
        elif state == "planning":
            system_context = (
                "This task is currently being planned. "
                "Answer the user's question and provide updates on planning progress."
            )
        elif state == "plan_ready":
            system_context = (
                "The execution plan is ready for review. "
                "Help the user understand the plan. They can approve or reject it."
            )
        elif state == "in_progress":
            system_context = (
                f"This task is being executed (sub-state: {sub_state}). "
                "Answer the user's question about progress or provide guidance."
            )
        else:
            system_context = (
                f"Current state: {state}. "
                "Answer the user's question about this task concisely."
            )

        system_prompt = (
            f"You are the AI coordinator for task: {todo['title']}.\n"
            f"Description: {todo['description'] or 'N/A'}\n"
            f"{system_context}\n"
            "Be concise and helpful."
        )

        # For linked sessions, add subtask context and task management tools
        action_tools = []
        action_context = {}
        if session_id:
            subtasks = await db.fetch(
                "SELECT id, title, agent_role, status, execution_order FROM sub_tasks "
                "WHERE todo_id = $1 ORDER BY execution_order, created_at",
                todo_id,
            )
            if subtasks:
                st_lines = [f"  - [{st['status']}] ({st['agent_role']}) {st['title']} (id: {st['id']})" for st in subtasks]
                system_prompt += "\n\nCurrent subtasks:\n" + "\n".join(st_lines)

            system_prompt += (
                "\n\nYou have task management tools available:\n"
                "- action__add_subtask — Add a new subtask\n"
                "- action__update_subtask — Update a pending subtask\n"
                "- action__remove_subtask — Remove a pending subtask\n"
                "- action__cancel_task — Cancel the task (confirm with user first)"
            )
            action_tools = get_actions_as_tools("session_task")
            action_context = {
                "db": db,
                "project_id": str(todo["project_id"]),
                "user_id": str(todo["creator_id"]),
                "todo_id": todo_id,
                "redis": redis,
                "event_bus": event_bus,
            }

        if skills_ctx:
            system_prompt += skills_ctx

        messages = [LLMMessage(role="system", content=system_prompt)]
        # Include recent chat history for context (last 30 messages)
        for row in chat_history[-30:]:
            messages.append(LLMMessage(role=row["role"], content=row["content"]))

        from agents.providers.base import run_tool_loop

        all_tools = action_tools + (mcp_tools or [])
        tools_arg = all_tools if all_tools else None
        mcp_exec = McpToolExecutor(db)

        async def _execute_tool(name: str, arguments: dict) -> str:
            if is_action_tool(name):
                return await execute_action(name, arguments, action_context)
            return await mcp_exec.execute_tool(name, arguments, mcp_tools)

        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=_execute_tool,
            max_rounds=5,
        )

        if session_id:
            # Linked session: write to project_chat_messages
            msg_row = await db.fetchrow(
                """
                INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
                VALUES ($1, $2, 'assistant', $3, $4) RETURNING *
                """,
                str(todo["project_id"]),
                str(todo["creator_id"]),
                content,
                session_id,
            )
        else:
            msg_row = await db.fetchrow(
                "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'assistant', $2) RETURNING *",
                todo_id,
                content,
            )

        chat_event = json.dumps({
            "type": "chat_message",
            "message": {"role": "assistant", "content": content, "id": str(msg_row["id"])},
        })
        await redis.publish(f"task:{todo_id}:events", chat_event)
        if session_id:
            await redis.publish(f"chat:session:{session_id}:activity", chat_event)
    except Exception:
        logger.exception("Failed to generate direct chat response for todo %s", todo_id)
