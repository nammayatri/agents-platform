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
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    rows = await db.fetch(
        "SELECT * FROM chat_messages WHERE todo_id = $1 ORDER BY created_at ASC",
        todo_id,
    )
    return [dict(r) for r in rows]


@router.post("/todos/{todo_id}/chat")
async def send_chat_message(
    todo_id: str, body: ChatMessageInput, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    todo = await db.fetchrow("SELECT state, sub_state, project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)

    # Store the message
    msg = await db.fetchrow(
        """
        INSERT INTO chat_messages (todo_id, role, content)
        VALUES ($1, 'user', $2) RETURNING *
        """,
        todo_id,
        body.content,
    )

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
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({
            "type": "chat_message",
            "message": {"role": "user", "content": body.content, "id": str(msg["id"])},
        }),
    )

    # Always generate a direct LLM response for the user -- don't make them
    # wait for the orchestrator poll cycle.  The orchestrator handles state
    # transitions; chat should always be fast and direct.
    asyncio.create_task(_direct_chat_response(todo_id, body.content, db, redis))

    return dict(msg)


async def _direct_chat_response(todo_id: str, message: str, db, redis):
    """Generate an immediate AI response to any user chat message."""
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry

    try:
        registry = ProviderRegistry(db)
        provider = await registry.resolve_for_todo(todo_id)

        todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
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
        if skills_ctx:
            system_prompt += skills_ctx

        messages = [LLMMessage(role="system", content=system_prompt)]
        # Include recent chat history for context (last 30 messages)
        for row in chat_history[-30:]:
            messages.append(LLMMessage(role=row["role"], content=row["content"]))

        from agents.providers.base import run_tool_loop

        tools_arg = mcp_tools if mcp_tools else None
        mcp_exec = McpToolExecutor(db)

        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=lambda name, args: mcp_exec.execute_tool(name, args, mcp_tools),
            max_rounds=5,
        )

        msg_row = await db.fetchrow(
            "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'assistant', $2) RETURNING *",
            todo_id,
            content,
        )
        await redis.publish(
            f"task:{todo_id}:events",
            json.dumps({
                "type": "chat_message",
                "message": {"role": "assistant", "content": content, "id": str(msg_row["id"])},
            }),
        )
    except Exception:
        logger.exception("Failed to generate direct chat response for todo %s", todo_id)
