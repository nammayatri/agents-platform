"""Scoped chat tool registry.

Tools are registered with a scope ('project', 'agent', 'task') and
only appear in the chat context that matches their scope.

Each tool is exposed to the LLM as a function/tool with name `action__<name>`.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Type alias for async handlers
ActionHandler = Callable[..., Coroutine[Any, Any, dict]]


@dataclass
class ChatAction:
    name: str
    description: str
    scope: str  # 'project' | 'agent' | 'task'
    input_schema: dict
    handler: ActionHandler
    tool_name: str = field(init=False)

    def __post_init__(self):
        self.tool_name = f"action__{self.name}"

    def as_tool_def(self) -> dict:
        """Return a tool definition suitable for LLM tool_use."""
        return {
            "name": self.tool_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# Global registry
_registry: list[ChatAction] = []


def chat_action(
    *,
    name: str,
    description: str,
    scope: str,
    input_schema: dict,
):
    """Decorator to register a chat action handler."""

    def decorator(fn: ActionHandler) -> ActionHandler:
        action = ChatAction(
            name=name,
            description=description,
            scope=scope,
            input_schema=input_schema,
            handler=fn,
        )
        _registry.append(action)
        return fn

    return decorator


def get_actions_as_tools(scope: str) -> list[dict]:
    """Return tool definitions for the given scope."""
    return [a.as_tool_def() for a in _registry if a.scope == scope]


def get_action_handler(tool_name: str) -> ActionHandler | None:
    """Look up a handler by its tool_name (e.g. 'action__create_task')."""
    for a in _registry:
        if a.tool_name == tool_name:
            return a.handler
    return None


def is_action_tool(tool_name: str) -> bool:
    """Check if a tool name belongs to the action registry."""
    return tool_name.startswith("action__")


async def execute_action(tool_name: str, arguments: dict, context: dict) -> str:
    """Execute a registered action and return the result as a string."""
    handler = get_action_handler(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown action: {tool_name}"})
    try:
        result = await handler(arguments, context)
        return json.dumps(result, default=str)
    except Exception as e:
        logger.exception("Action %s failed", tool_name)
        return json.dumps({"error": str(e)})


# ── Built-in actions ──────────────────────────────────────────


@chat_action(
    name="create_task",
    description="Create a new task for the project. Use when the user describes work they want done.",
    scope="project",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Task title"},
            "description": {"type": "string", "description": "Detailed task description"},
            "priority": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low"],
                "description": "Task priority",
            },
            "task_type": {
                "type": "string",
                "enum": ["code", "research", "document", "general"],
                "description": "Type of task",
            },
            "sub_tasks": {
                "type": "array",
                "description": (
                    "Pre-planned sub-tasks. When provided, the task skips intake/planning "
                    "and goes directly to execution."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "agent_role": {"type": "string"},
                        "execution_order": {"type": "integer"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "review_loop": {
                            "type": "boolean",
                            "description": "Set true for critical code changes needing coder→reviewer→merge cycle",
                        },
                    },
                    "required": ["title", "agent_role"],
                },
            },
        },
        "required": ["title"],
    },
)
async def _handle_create_task(arguments: dict, context: dict) -> dict:
    """Create a todo item. Context must include db, project_id, user_id, event_bus."""
    db = context["db"]
    project_id = context["project_id"]
    user_id = context["user_id"]
    event_bus = context.get("event_bus")
    sub_tasks_input = arguments.get("sub_tasks") or []

    if sub_tasks_input:
        # Direct execution path: create task in in_progress with sub-tasks
        plan_json = {
            "summary": arguments.get("description", arguments["title"]),
            "sub_tasks": [
                {
                    "title": st["title"],
                    "description": st.get("description", ""),
                    "agent_role": st["agent_role"],
                    "execution_order": st.get("execution_order", i),
                    "depends_on": st.get("depends_on", []),
                    "review_loop": bool(st.get("review_loop", False)),
                }
                for i, st in enumerate(sub_tasks_input)
            ],
        }
        intake_data = {
            "requirements": arguments.get("description", arguments["title"]),
            "approach": "Pre-planned by planner agent",
        }

        todo = await db.fetchrow(
            """
            INSERT INTO todo_items (
                project_id, creator_id, title, description, priority, task_type,
                state, sub_state, plan_json, intake_data
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'in_progress', 'executing', $7, $8)
            RETURNING *
            """,
            project_id,
            user_id,
            arguments["title"],
            arguments.get("description", ""),
            arguments.get("priority", "medium"),
            arguments.get("task_type", "general"),
            plan_json,
            intake_data,
        )
        todo_id = str(todo["id"])

        # Insert sub-tasks into the sub_tasks table
        sub_task_ids = []
        for i, st in enumerate(plan_json["sub_tasks"]):
            review_loop = bool(st.get("review_loop", False))
            row = await db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, input_context, review_loop
                )
                VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, $6)
                RETURNING id
                """,
                todo_id,
                st["title"],
                st.get("description", ""),
                st["agent_role"],
                st.get("execution_order", 0),
                review_loop,
            )
            sub_task_ids.append(str(row["id"]))

            # For review_loop sub-tasks, set review_chain_id to themselves (chain root)
            if review_loop:
                await db.execute(
                    "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                    row["id"],
                )

        # Set up depends_on using index→UUID mapping
        for i, st in enumerate(plan_json["sub_tasks"]):
            deps = st.get("depends_on", [])
            if deps:
                dep_ids = [sub_task_ids[j] for j in deps if j < len(sub_task_ids)]
                if dep_ids:
                    await db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        sub_task_ids[i],
                        dep_ids,
                    )

        # Link to chat session if created from a session
        session_id = context.get("session_id")
        if session_id:
            await db.execute(
                "UPDATE todo_items SET chat_session_id = $1 WHERE id = $2",
                session_id, todo_id,
            )
            await db.execute(
                "UPDATE project_chat_sessions SET linked_todo_id = $1 WHERE id = $2",
                todo_id, session_id,
            )

        # Emit event for orchestrator — task starts at in_progress
        if event_bus:
            try:
                from agents.orchestrator.events import TaskEvent

                await event_bus.publish(TaskEvent(
                    event_type="task_created",
                    todo_id=todo_id,
                    state="in_progress",
                ))
            except Exception:
                logger.warning("Failed to emit event for direct-execution task %s", todo_id[:8])

        return {
            "action": "task_created",
            "task_id": todo_id,
            "title": arguments["title"],
            "priority": arguments.get("priority", "medium"),
            "task_type": arguments.get("task_type", "general"),
            "sub_tasks_created": len(sub_task_ids),
            "direct_execution": True,
            "linked_session_id": session_id,
        }

    # Standard path: create task in intake state
    todo = await db.fetchrow(
        """
        INSERT INTO todo_items (project_id, creator_id, title, description, priority, task_type)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        project_id,
        user_id,
        arguments["title"],
        arguments.get("description", ""),
        arguments.get("priority", "medium"),
        arguments.get("task_type", "general"),
    )
    todo_id = str(todo["id"])

    # Link to chat session if created from a session
    session_id = context.get("session_id")
    if session_id:
        await db.execute(
            "UPDATE todo_items SET chat_session_id = $1 WHERE id = $2",
            session_id, todo_id,
        )
        await db.execute(
            "UPDATE project_chat_sessions SET linked_todo_id = $1 WHERE id = $2",
            todo_id, session_id,
        )

    # Emit event for orchestrator pickup
    if event_bus:
        try:
            from agents.orchestrator.events import TaskEvent

            await event_bus.publish(TaskEvent(
                event_type="task_created",
                todo_id=todo_id,
                state="intake",
            ))
        except Exception:
            pass

    return {
        "action": "task_created",
        "task_id": todo_id,
        "title": arguments["title"],
        "priority": arguments.get("priority", "medium"),
        "task_type": arguments.get("task_type", "general"),
        "linked_session_id": session_id,
    }


@chat_action(
    name="delete_task",
    description=(
        "Delete an existing task from the project. IMPORTANT: You MUST always ask "
        "the user to confirm before calling this tool. Show the task title and ask "
        "'Are you sure you want to delete this task?' Only call this after the user "
        "explicitly confirms with 'yes', 'confirm', 'do it', etc."
    ),
    scope="project",
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "UUID of the task to delete"},
            "title": {"type": "string", "description": "Title of the task (for confirmation logging)"},
        },
        "required": ["task_id"],
    },
)
async def _handle_delete_task(arguments: dict, context: dict) -> dict:
    """Delete a todo item. Context must include db, project_id, user_id."""
    db = context["db"]
    project_id = context["project_id"]

    task_id = arguments["task_id"]

    # Verify the task exists and belongs to this project
    todo = await db.fetchrow(
        "SELECT id, title, state FROM todo_items WHERE id = $1 AND project_id = $2",
        task_id,
        project_id,
    )
    if not todo:
        return {"error": f"Task {task_id} not found in this project"}

    # Cancel the task first if it's running
    if todo["state"] in ("intake", "planning", "in_progress", "review"):
        await db.execute(
            "UPDATE todo_items SET state = 'cancelled', state_changed_at = NOW() WHERE id = $1",
            task_id,
        )

    # Delete associated records then the task
    await db.execute("DELETE FROM chat_messages WHERE todo_id = $1", task_id)
    await db.execute("DELETE FROM deliverables WHERE todo_id = $1", task_id)
    await db.execute("DELETE FROM agent_runs WHERE todo_id = $1", task_id)
    await db.execute("DELETE FROM sub_tasks WHERE todo_id = $1", task_id)
    await db.execute("DELETE FROM todo_items WHERE id = $1", task_id)

    return {
        "action": "task_deleted",
        "task_id": task_id,
        "title": todo["title"],
    }


# ── Session-Task actions (linked session → task management) ───


@chat_action(
    name="add_subtask",
    description=(
        "Add a new subtask to the linked task. The subtask will be created in pending status. "
        "Use when the user wants to add more work to an existing task."
    ),
    scope="session_task",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Subtask title"},
            "description": {"type": "string", "description": "Detailed description of the subtask"},
            "agent_role": {
                "type": "string",
                "enum": ["coder", "tester", "reviewer", "debugger", "report_writer"],
                "description": "Agent role to execute this subtask",
            },
            "execution_order": {
                "type": "integer",
                "description": "Execution order (0 = parallel with others at order 0)",
            },
        },
        "required": ["title", "agent_role"],
    },
)
async def _handle_add_subtask(arguments: dict, context: dict) -> dict:
    """Add a subtask to the linked task."""
    db = context["db"]
    todo_id = context.get("todo_id")
    if not todo_id:
        return {"error": "No linked task found for this session"}

    todo = await db.fetchrow(
        "SELECT id, title, state FROM todo_items WHERE id = $1", todo_id,
    )
    if not todo:
        return {"error": f"Task {todo_id} not found"}

    if todo["state"] in ("completed", "failed", "cancelled"):
        return {"error": f"Cannot add subtasks to a task in '{todo['state']}' state"}

    row = await db.fetchrow(
        """
        INSERT INTO sub_tasks (todo_id, title, description, agent_role, execution_order, input_context)
        VALUES ($1, $2, $3, $4, $5, '{}'::jsonb)
        RETURNING id, title, agent_role, status
        """,
        todo_id,
        arguments["title"],
        arguments.get("description", ""),
        arguments["agent_role"],
        arguments.get("execution_order", 0),
    )

    return {
        "action": "subtask_added",
        "task_id": todo_id,
        "sub_task_id": str(row["id"]),
        "title": arguments["title"],
        "agent_role": arguments["agent_role"],
    }


@chat_action(
    name="update_subtask",
    description=(
        "Update an existing subtask that hasn't started yet (pending status only). "
        "Use to change title, description, or agent role of a pending subtask."
    ),
    scope="session_task",
    input_schema={
        "type": "object",
        "properties": {
            "sub_task_id": {"type": "string", "description": "UUID of the subtask to update"},
            "title": {"type": "string", "description": "New title"},
            "description": {"type": "string", "description": "New description"},
            "agent_role": {
                "type": "string",
                "enum": ["coder", "tester", "reviewer", "debugger", "report_writer"],
                "description": "New agent role",
            },
        },
        "required": ["sub_task_id"],
    },
)
async def _handle_update_subtask(arguments: dict, context: dict) -> dict:
    """Update a pending subtask."""
    db = context["db"]
    todo_id = context.get("todo_id")
    if not todo_id:
        return {"error": "No linked task found for this session"}

    sub_task_id = arguments["sub_task_id"]
    st = await db.fetchrow(
        "SELECT id, title, status FROM sub_tasks WHERE id = $1 AND todo_id = $2",
        sub_task_id, todo_id,
    )
    if not st:
        return {"error": f"Subtask {sub_task_id} not found in this task"}

    if st["status"] != "pending":
        return {"error": f"Cannot update subtask in '{st['status']}' status (must be pending)"}

    updates = []
    params = []
    idx = 1
    for field in ("title", "description", "agent_role"):
        if field in arguments:
            updates.append(f"{field} = ${idx}")
            params.append(arguments[field])
            idx += 1

    if not updates:
        return {"error": "No fields to update"}

    params.append(sub_task_id)
    await db.execute(
        f"UPDATE sub_tasks SET {', '.join(updates)} WHERE id = ${idx}",
        *params,
    )

    return {
        "action": "subtask_updated",
        "task_id": todo_id,
        "sub_task_id": sub_task_id,
        "updated_fields": [f for f in ("title", "description", "agent_role") if f in arguments],
    }


@chat_action(
    name="remove_subtask",
    description=(
        "Remove a subtask that hasn't started yet (pending status only). "
        "Use when the user wants to remove planned work from the task."
    ),
    scope="session_task",
    input_schema={
        "type": "object",
        "properties": {
            "sub_task_id": {"type": "string", "description": "UUID of the subtask to remove"},
        },
        "required": ["sub_task_id"],
    },
)
async def _handle_remove_subtask(arguments: dict, context: dict) -> dict:
    """Remove a pending subtask."""
    db = context["db"]
    todo_id = context.get("todo_id")
    if not todo_id:
        return {"error": "No linked task found for this session"}

    sub_task_id = arguments["sub_task_id"]
    st = await db.fetchrow(
        "SELECT id, title, status FROM sub_tasks WHERE id = $1 AND todo_id = $2",
        sub_task_id, todo_id,
    )
    if not st:
        return {"error": f"Subtask {sub_task_id} not found in this task"}

    if st["status"] != "pending":
        return {"error": f"Cannot remove subtask in '{st['status']}' status (must be pending)"}

    await db.execute("DELETE FROM sub_tasks WHERE id = $1", sub_task_id)

    return {
        "action": "subtask_removed",
        "task_id": todo_id,
        "sub_task_id": sub_task_id,
        "title": st["title"],
    }


@chat_action(
    name="cancel_task",
    description=(
        "Cancel the linked task and stop all running work. "
        "IMPORTANT: Always confirm with the user before cancelling."
    ),
    scope="session_task",
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Reason for cancellation"},
        },
    },
)
async def _handle_cancel_task(arguments: dict, context: dict) -> dict:
    """Cancel the linked task and all its non-terminal subtasks."""
    db = context["db"]
    todo_id = context.get("todo_id")
    if not todo_id:
        return {"error": "No linked task found for this session"}

    todo = await db.fetchrow(
        "SELECT id, title, state FROM todo_items WHERE id = $1", todo_id,
    )
    if not todo:
        return {"error": f"Task {todo_id} not found"}

    if todo["state"] in ("completed", "cancelled"):
        return {"error": f"Task is already {todo['state']}"}

    # Transition the task to cancelled
    await db.execute(
        "UPDATE todo_items SET state = 'cancelled', state_changed_at = NOW(), updated_at = NOW() WHERE id = $1",
        todo_id,
    )

    # Cancel all non-terminal subtasks
    cancelled_count = await db.fetchval(
        """
        WITH updated AS (
            UPDATE sub_tasks SET status = 'cancelled', updated_at = NOW()
            WHERE todo_id = $1 AND status NOT IN ('completed', 'failed', 'cancelled')
            RETURNING id
        )
        SELECT count(*) FROM updated
        """,
        todo_id,
    )

    # Publish cancellation event via Redis
    event_bus = context.get("event_bus")
    redis = context.get("redis")
    if redis:
        await redis.publish(
            f"task:{todo_id}:events",
            json.dumps({"type": "task_cancelled"}),
        )

    return {
        "action": "task_cancelled",
        "task_id": todo_id,
        "title": todo["title"],
        "subtasks_cancelled": cancelled_count or 0,
        "reason": arguments.get("reason", ""),
    }
