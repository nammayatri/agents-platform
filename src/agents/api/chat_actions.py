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
            VALUES ($1, $2, $3, $4, $5, $6, 'in_progress', 'executing', $7::jsonb, $8::jsonb)
            RETURNING *
            """,
            project_id,
            user_id,
            arguments["title"],
            arguments.get("description", ""),
            arguments.get("priority", "medium"),
            arguments.get("task_type", "general"),
            json.dumps(plan_json),
            json.dumps(intake_data),
        )
        todo_id = str(todo["id"])

        # Insert sub-tasks into the sub_tasks table
        sub_task_ids = []
        for i, st in enumerate(plan_json["sub_tasks"]):
            row = await db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, input_context
                )
                VALUES ($1, $2, $3, $4, $5, '{}'::jsonb)
                RETURNING id
                """,
                todo_id,
                st["title"],
                st.get("description", ""),
                st["agent_role"],
                st.get("execution_order", 0),
            )
            sub_task_ids.append(str(row["id"]))

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

    # Emit event for orchestrator pickup
    if event_bus:
        try:
            from agents.orchestrator.events import TaskEvent

            await event_bus.publish(TaskEvent(
                event_type="task_created",
                todo_id=str(todo["id"]),
                state="intake",
            ))
        except Exception:
            pass

    return {
        "action": "task_created",
        "task_id": str(todo["id"]),
        "title": arguments["title"],
        "priority": arguments.get("priority", "medium"),
        "task_type": arguments.get("task_type", "general"),
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
