"""Project-level chat: natural language interface for creating tasks,
asking questions about the project, debugging, etc.

Supports multi-session chat and plan mode."""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.api.chat_actions import execute_action, get_actions_as_tools, is_action_tool
from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access
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
    plan_mode = body.mode == "plan"
    row = await db.fetchrow(
        """
        INSERT INTO project_chat_sessions (project_id, user_id, title, mode, plan_mode)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        project_id,
        user["id"],
        title,
        body.mode,
        plan_mode,
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
    return {
        **dict(session),
        "messages": [dict(m) for m in messages],
    }


@router.put("/projects/{project_id}/chat/sessions/{session_id}")
async def update_session(
    project_id: str, session_id: str, body: SessionUpdateInput, user: CurrentUser, db: DB
):
    """Update session title."""
    session = await _load_session(session_id, project_id, user, db)
    if body.title is not None:
        await db.execute(
            "UPDATE project_chat_sessions SET title = $2, updated_at = NOW() WHERE id = $1",
            session_id,
            body.title,
        )
    row = await db.fetchrow("SELECT * FROM project_chat_sessions WHERE id = $1", session_id)
    return dict(row)


@router.post("/projects/{project_id}/chat/sessions/{session_id}/toggle-plan")
async def toggle_plan_mode(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Toggle plan mode on/off for a session."""
    session = await _load_session(session_id, project_id, user, db)
    current = session.get("plan_mode", session.get("mode") == "plan")
    new_value = not current
    await db.execute(
        "UPDATE project_chat_sessions SET plan_mode = $2, updated_at = NOW() WHERE id = $1",
        session_id,
        new_value,
    )
    return {"plan_mode": new_value}


@router.post("/projects/{project_id}/chat/sessions/{session_id}/mode")
async def set_chat_mode(
    project_id: str, session_id: str, body: dict, user: CurrentUser, db: DB
):
    """Set the chat mode for a session (chat, plan, debug, create_task)."""
    await _load_session(session_id, project_id, user, db)
    mode = body.get("mode", "chat")
    if mode not in ("chat", "plan", "debug", "create_task"):
        raise HTTPException(status_code=400, detail="Invalid mode")

    plan_mode = mode == "plan"
    await db.execute(
        "UPDATE project_chat_sessions SET chat_mode = $2, plan_mode = $3, updated_at = NOW() WHERE id = $1",
        session_id,
        mode,
        plan_mode,
    )
    return {"chat_mode": mode, "plan_mode": plan_mode}


@router.delete("/projects/{project_id}/chat/sessions/{session_id}")
async def delete_session(project_id: str, session_id: str, user: CurrentUser, db: DB):
    """Delete a session and all its messages."""
    await _load_session(session_id, project_id, user, db)
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
        # Resolve model override: explicit body.model > session persisted > None
        model_override = body.model or dict(session).get("ai_model")

        # Persist model selection to session if explicitly provided
        if body.model:
            await db.execute(
                "UPDATE project_chat_sessions SET ai_model = $2, updated_at = NOW() WHERE id = $1",
                session_id, body.model,
            )

        # Resolve chat mode: prefer chat_mode column, fall back to plan_mode
        chat_mode = dict(session).get("chat_mode", "chat")
        if chat_mode == "chat" and dict(session).get("plan_mode"):
            chat_mode = "plan"

        if chat_mode == "plan":
            assistant_msg = await _generate_plan_response(
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
            assistant_msg = await _generate_debug_response(
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
            assistant_msg = await _generate_create_task_response(
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
            assistant_msg = await _generate_project_response(
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


# ── Legacy endpoints (backward compat, use default session) ───────


@router.get("/projects/{project_id}/chat")
async def get_project_chat(project_id: str, user: CurrentUser, db: DB):
    """Get chat history for a project (legacy: returns all non-session messages + default session)."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        "SELECT * FROM project_chat_messages WHERE project_id = $1 AND user_id = $2 "
        "AND session_id IS NULL "
        "ORDER BY created_at ASC LIMIT 200",
        project_id,
        user["id"],
    )
    return [dict(r) for r in rows]


@router.delete("/projects/{project_id}/chat")
async def clear_project_chat(project_id: str, user: CurrentUser, db: DB):
    """Clear legacy chat history."""
    await check_project_access(db, project_id, user)
    await db.execute(
        "DELETE FROM project_chat_messages WHERE project_id = $1 AND user_id = $2 AND session_id IS NULL",
        project_id,
        user["id"],
    )
    return {"status": "cleared"}


@router.delete("/projects/{project_id}/chat/{message_id}")
async def delete_chat_message(project_id: str, message_id: str, user: CurrentUser, db: DB):
    """Delete a single chat message. If it created a task, also delete the task."""
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
    project_id: str,
    body: ProjectChatInput,
    user: CurrentUser,
    db: DB,
    event_bus: EventBusDep,
):
    """Send a chat message (legacy: uses no session)."""
    await check_project_access(db, project_id, user)
    project = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not project:
        raise HTTPException(status_code=404)

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
        assistant_msg = await _generate_project_response(
            project_id=project_id,
            session_id=None,
            user_id=str(user["id"]),
            user_message=body.content,
            intent=body.intent,
            project=dict(project),
            db=db,
            event_bus=event_bus,
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


# ── Helpers ───────────────────────────────────────────────────────


async def _check_project_access_local(project_id: str, user: dict, db) -> dict:
    """Verify access and return the project row."""
    await check_project_access(db, project_id, user)
    project = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
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


async def _resolve_planner_config(db, user_id: str) -> dict | None:
    """Look up a custom planner agent config for the user."""
    row = await db.fetchrow(
        "SELECT * FROM agent_configs WHERE role = 'planner' AND owner_id = $1 AND is_active = TRUE "
        "ORDER BY updated_at DESC LIMIT 1",
        user_id,
    )
    return dict(row) if row else None


async def _generate_project_response(
    *,
    project_id: str,
    session_id: str | None,
    user_id: str,
    user_message: str,
    intent: str | None,
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
) -> dict:
    """Generate an AI response for a project-level chat message."""
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    # Resolve planner agent config (custom prompt/model)
    planner_config = await _resolve_planner_config(db, user_id)

    # Resolve MCP tools and skills for this project
    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )
    skills_ctx = await tools_reg.build_skills_context(
        project_id=project_id, user_id=user_id,
    )

    # Check if session is linked to a task — switch tool scope accordingly
    linked_todo_id = None
    if session_id:
        session_row = await db.fetchrow(
            "SELECT linked_todo_id FROM project_chat_sessions WHERE id = $1", session_id,
        )
        if session_row and session_row["linked_todo_id"]:
            linked_todo_id = str(session_row["linked_todo_id"])

    if linked_todo_id:
        # Linked session: provide task management tools instead of create/delete
        action_tools = get_actions_as_tools("session_task")
    else:
        # Scoped action tools for project chat (create_task, delete_task only)
        action_tools = get_actions_as_tools("project")

    # Builtin workspace tools (read_file, list_directory, search_files) for
    # codebase exploration — same pattern the coordinator uses for agents.
    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "planner") if workspace_path else []

    # Fetch recent chat history
    if session_id:
        history = await db.fetch(
            "SELECT role, content FROM project_chat_messages "
            "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 30",
            session_id,
        )
    else:
        history = await db.fetch(
            "SELECT role, content FROM project_chat_messages "
            "WHERE project_id = $1 AND user_id = $2 AND session_id IS NULL "
            "ORDER BY created_at DESC LIMIT 30",
            project_id,
            user_id,
        )
    history = list(reversed(history))

    # Fetch existing tasks for context
    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 20",
        project_id,
    )
    tasks_ctx = ""
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        tasks_ctx = "\n\nExisting tasks:\n" + "\n".join(task_lines)

    # Fetch project understanding and work rules
    project_ctx = ""
    settings = safe_json(project.get("settings_json"))
    if settings:
        understanding = settings.get("project_understanding", {})
        if understanding:
            if understanding.get("summary"):
                project_ctx += f"\nProject summary: {understanding['summary']}"
            if understanding.get("tech_stack"):
                project_ctx += f"\nTech stack: {', '.join(understanding['tech_stack'])}"
        work_rules = settings.get("work_rules", {})
        if work_rules:
            rules_parts = []
            for cat, items in work_rules.items():
                if items:
                    rules_parts.append(f"  {cat}: {', '.join(items)}")
            if rules_parts:
                project_ctx += "\n\nCurrent work rules:\n" + "\n".join(rules_parts)

        # Include debug context summary so planner knows debugging capabilities
        debug_ctx = settings.get("debug_context", {})
        if debug_ctx:
            debug_parts = []
            for src in debug_ctx.get("log_sources", []):
                debug_parts.append(f"  - {src.get('service_name', 'service')}: {src.get('description', 'logs available')}")
            for hint in debug_ctx.get("mcp_hints", []):
                data = ", ".join(hint.get("available_data", []))
                debug_parts.append(f"  - {hint.get('mcp_server_name', 'MCP')}: {data}")
            if debug_parts:
                project_ctx += "\n\nDebug context (use debugger agent for bug investigations):\n" + "\n".join(debug_parts)

    # Use custom planner prompt if configured, otherwise default from registry
    if planner_config and planner_config.get("system_prompt"):
        base_prompt = planner_config["system_prompt"]
    else:
        from agents.agents.registry import get_default_system_prompt
        base_prompt = get_default_system_prompt("planner")

    # Build dynamic tools documentation for the system prompt
    if linked_todo_id:
        # Linked session: show task management tools instead of create/delete
        linked_todo = await db.fetchrow(
            "SELECT title, state, sub_state FROM todo_items WHERE id = $1", linked_todo_id,
        )
        linked_subtasks = await db.fetch(
            "SELECT id, title, agent_role, status, execution_order FROM sub_tasks "
            "WHERE todo_id = $1 ORDER BY execution_order, created_at",
            linked_todo_id,
        )
        linked_task_ctx = f"\n\nLinked task: \"{linked_todo['title']}\" [{linked_todo['state']}]"
        if linked_subtasks:
            st_lines = [f"  - [{st['status']}] ({st['agent_role']}) {st['title']} (id: {st['id']})" for st in linked_subtasks]
            linked_task_ctx += "\nSubtasks:\n" + "\n".join(st_lines)
        tasks_ctx = linked_task_ctx + tasks_ctx

        tools_doc = """
You have the following task management actions for the linked task:
- **action__add_subtask** — Add a new subtask (pending status). Specify title, agent_role, and optionally description.
- **action__update_subtask** — Update a pending subtask's title, description, or agent_role. Requires sub_task_id.
- **action__remove_subtask** — Remove a pending subtask. Requires sub_task_id.
- **action__cancel_task** — Cancel the task and stop all running work. ALWAYS confirm with the user first."""
    else:
        tools_doc = """
You have the following actions available:
- **action__create_task** — Create a new tracked task. Use when the user describes work they want done. \
You can include sub_tasks to send work directly to execution, or omit them for the intake/planning pipeline. \
IMPORTANT: Always create ONE task with ALL sub_tasks inside it. Never create multiple separate tasks for related work.
- **action__delete_task** — Delete a task. ALWAYS ask for user confirmation before calling this."""

    if builtin_tools:
        tools_doc += """

You ALSO have workspace tools to explore the codebase — USE THEM to research before planning:
- **read_file** — Read a file's contents (path relative to repo root)
- **list_directory** — List files and directories (path relative to repo root, empty for root)
- **search_files** — Search for a text pattern across files (grep)
- **run_command** — Run a shell command in the repo root. Use for git, gh (GitHub CLI), builds, tests, etc.
  Examples: `git log --oneline -10`, `git status`, `gh issue list`, `gh pr list`, `gh pr view 42`

IMPORTANT: Always explore the codebase with these tools before creating tasks. \
Read relevant files, understand the existing code structure, and then plan accordingly. \
Do NOT guess or assume what the codebase looks like — read it first. \
Use run_command with git/gh to check repo status, open PRs, issues, and branches."""
    else:
        tools_doc += "\n\nNote: No workspace is configured for this project, so codebase exploration tools are not available."

    tools_doc += "\n\nDo NOT directly make code changes — create tasks instead."

    system_prompt = f"""{base_prompt}

Project: "{project['name']}"
{f"Description: {project['description']}" if project.get("description") else ""}
{f"Repository: {project['repo_url']}" if project.get("repo_url") else ""}{project_ctx}{tasks_ctx}
{tools_doc}"""

    if skills_ctx:
        system_prompt += skills_ctx

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        messages.append(LLMMessage(role=row["role"], content=row["content"]))
    messages.append(LLMMessage(role="user", content=user_message))

    from agents.providers.base import run_tool_loop

    # Combine: action tools + builtin workspace tools + MCP tools
    all_tools = (action_tools or []) + builtin_tools + (mcp_tools or [])
    tools_arg = all_tools if all_tools else None

    # Build a tool executor that routes to action handlers or MCP
    action_context = {
        "db": db,
        "project_id": project_id,
        "user_id": user_id,
        "event_bus": event_bus,
        "session_id": session_id,
        "redis": redis,
    }
    if linked_todo_id:
        action_context["todo_id"] = linked_todo_id
    mcp_exec = McpToolExecutor(db)
    metadata = None

    async def _execute_tool(name: str, arguments: dict) -> str:
        nonlocal metadata
        if is_action_tool(name):
            result_text = await execute_action(name, arguments, action_context)
            try:
                result_data = json.loads(result_text)
                if result_data.get("action") in ("task_created", "task_deleted"):
                    metadata = {
                        "action": result_data["action"],
                        "task_id": result_data.get("task_id"),
                        "task_title": result_data.get("title"),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
            return result_text
        return await mcp_exec.execute_tool(name, arguments, builtin_tools + (mcp_tools or []))

    # Build on_activity callback to stream tool execution updates via Redis
    on_activity = None
    if redis and session_id:
        async def on_activity(msg: str) -> None:
            await redis.publish(
                f"chat:session:{session_id}:activity",
                json.dumps({"type": "activity", "activity": msg}),
            )

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override
    elif planner_config and planner_config.get("model_preference"):
        send_kwargs["model"] = planner_config["model_preference"]

    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=5,
        on_activity=on_activity,
        **send_kwargs,
    )

    # Store assistant response
    msg_row = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'assistant', $3, $4::jsonb, $5) RETURNING *
        """,
        project_id,
        user_id,
        content,
        json.dumps(metadata) if metadata else None,
        session_id,
    )
    return dict(msg_row)


# ── Plan Mode ─────────────────────────────────────────────────────


PLAN_MODE_SYSTEM = """\
You are a project planning assistant for "{project_name}".
{project_context}

You are in PLANNING MODE. Your job is to:
1. Discuss the project scope, requirements, and technical approach with the user
2. Ask clarifying questions to understand the full picture
3. Progressively build a structured plan with subtasks

When you and the user agree on a plan, output it as a structured JSON block.
CRITICAL: Always create exactly ONE task with ALL subtasks inside it. \
Dependencies between separate tasks are NOT supported — only depends_on between subtasks within the SAME task works.

```json
{{
  "action": "create_plan",
  "plan_title": "...",
  "tasks": [
    {{
      "title": "Overall task title",
      "description": "Full description of the entire scope of work",
      "priority": "medium",
      "task_type": "code",
      "subtasks": [
        {{
          "title": "First subtask",
          "description": "...",
          "agent_role": "coder",
          "depends_on": [],
          "parallel": false
        }},
        {{
          "title": "Second subtask (depends on first)",
          "description": "...",
          "agent_role": "coder",
          "depends_on": [0],
          "parallel": false
        }}
      ]
    }}
  ]
}}
```

Guidelines:
- The `tasks` array should contain exactly ONE task — put ALL work as subtasks with depends_on
- Each subtask should be a focused unit of work (one file, one feature, one concern)
- Use `depends_on` (list of 0-based subtask indexes) to express ordering between subtasks
- Subtasks with no dependencies and `parallel: true` can run concurrently
- Valid agent roles: coder, tester, reviewer, pr_creator, report_writer
- Valid task types: code, research, document, general
- Valid priorities: critical, high, medium, low
- Keep discussing until the user confirms the plan
- You can output intermediate plans for review — the user will say "looks good" or give feedback
"""


async def _generate_plan_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    project: dict,
    session: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
) -> dict:
    """Generate an AI response in plan mode."""
    from agents.providers.registry import ProviderRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    # Resolve planner agent config for model override
    planner_config = await _resolve_planner_config(db, user_id)

    # Build project context
    project_ctx_parts = []
    if project.get("description"):
        project_ctx_parts.append(f"Description: {project['description']}")
    if project.get("repo_url"):
        project_ctx_parts.append(f"Repository: {project['repo_url']}")

    settings = safe_json(project.get("settings_json"))
    if settings:
        understanding = settings.get("project_understanding", {})
        if understanding.get("summary"):
            project_ctx_parts.append(f"Summary: {understanding['summary']}")
        if understanding.get("tech_stack"):
            project_ctx_parts.append(f"Tech stack: {', '.join(understanding['tech_stack'])}")

        # Debug context summary
        debug_ctx = settings.get("debug_context", {})
        if debug_ctx:
            debug_parts = []
            for src in debug_ctx.get("log_sources", []):
                debug_parts.append(f"  - {src.get('service_name', 'service')}: {src.get('description', 'logs available')}")
            for hint in debug_ctx.get("mcp_hints", []):
                data = ", ".join(hint.get("available_data", []))
                debug_parts.append(f"  - {hint.get('mcp_server_name', 'MCP')}: {data}")
            if debug_parts:
                project_ctx_parts.append("Debug context (use debugger agent for bug investigations):\n" + "\n".join(debug_parts))

    # Existing tasks
    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 20",
        project_id,
    )
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        project_ctx_parts.append("Existing tasks:\n" + "\n".join(task_lines))

    project_context = "\n".join(project_ctx_parts)

    system_prompt = PLAN_MODE_SYSTEM.format(
        project_name=project["name"],
        project_context=project_context,
    )

    # Load session history
    history = await db.fetch(
        "SELECT role, content FROM project_chat_messages "
        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 40",
        session_id,
    )
    history = list(reversed(history))

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        messages.append(LLMMessage(role=row["role"], content=row["content"]))
    messages.append(LLMMessage(role="user", content=user_message))

    plan_send_kwargs: dict = {}
    if model_override:
        plan_send_kwargs["model"] = model_override
    elif planner_config and planner_config.get("model_preference"):
        plan_send_kwargs["model"] = planner_config["model_preference"]

    if redis and session_id:
        await redis.publish(
            f"chat:session:{session_id}:activity",
            json.dumps({"type": "activity", "activity": "Generating plan response..."}),
        )

    response = await provider.send_message(messages, **plan_send_kwargs)
    content = response.content  # already sanitized by LLMResponse

    # Check if the response contains a plan creation action
    metadata = None
    if "```json" in content and '"action"' in content and '"create_plan"' in content:
        try:
            json_start = content.index("```json") + 7
            json_end = content.index("```", json_start)
            plan_data = json.loads(content[json_start:json_end].strip())

            if plan_data.get("action") == "create_plan":
                # Store plan in session
                await db.execute(
                    "UPDATE project_chat_sessions SET plan_json = $2::jsonb, updated_at = NOW() WHERE id = $1",
                    session_id,
                    json.dumps(plan_data),
                )
                metadata = {
                    "action": "plan_proposed",
                    "plan_title": plan_data.get("plan_title", ""),
                    "task_count": len(plan_data.get("tasks", [])),
                    "plan_data": plan_data,
                }
        except (ValueError, json.JSONDecodeError, KeyError):
            pass

    # Check if user accepted the plan (look for accept keywords)
    plan_json = session.get("plan_json")
    if plan_json and _is_plan_acceptance(user_message):
        plan_json = safe_json(plan_json) if isinstance(plan_json, str) else plan_json

        created_tasks = await _create_tasks_from_plan(
            project_id=project_id,
            user_id=user_id,
            plan=plan_json,
            db=db,
            event_bus=event_bus,
            session_id=session_id,
        )

        # Auto-exit plan mode after tasks are created
        await db.execute(
            "UPDATE project_chat_sessions SET plan_mode = FALSE, updated_at = NOW() WHERE id = $1",
            session_id,
        )

        metadata = {
            "action": "plan_accepted",
            "plan_mode": False,
            "tasks_created": len(created_tasks),
            "task_ids": created_tasks,
        }

        # Append acceptance info to content
        task_summary = "\n".join(f"  - {t['title']}" for t in plan_json.get("tasks", []))
        content = (
            f"{content}\n\n"
            f"**Plan accepted!** Created {len(created_tasks)} tasks:\n{task_summary}"
        )

    # Store assistant response
    msg_row = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'assistant', $3, $4::jsonb, $5) RETURNING *
        """,
        project_id,
        user_id,
        content,
        json.dumps(metadata) if metadata else None,
        session_id,
    )
    return dict(msg_row)


def _is_plan_acceptance(message: str) -> bool:
    """Check if the user message is accepting the proposed plan."""
    lower = message.lower().strip()
    accept_phrases = [
        "looks good", "approve", "accept", "go ahead", "lgtm",
        "ship it", "proceed", "let's do it", "start", "yes",
        "perfect", "do it", "execute", "run it",
    ]
    return any(phrase in lower for phrase in accept_phrases)


async def _create_tasks_from_plan(
    *, project_id: str, user_id: str, plan: dict, db, event_bus=None, session_id: str | None = None,
) -> list[str]:
    """Create todo items from a plan.

    When subtasks are defined, creates the task directly in 'in_progress'
    with sub_tasks inserted into the DB, so the coordinator picks it up
    at the execution phase immediately.
    """
    created_ids = []
    session_linked = False  # Only link the first task to the session
    for task in plan.get("tasks", []):
        subtasks = task.get("subtasks", [])

        if subtasks:
            # Direct execution: create task in in_progress with sub-tasks
            plan_json = {
                "summary": task.get("description", task["title"]),
                "sub_tasks": [
                    {
                        "title": st["title"],
                        "description": st.get("description", ""),
                        "agent_role": st.get("agent_role", "coder"),
                        "execution_order": i if not st.get("parallel") else 0,
                        "depends_on": st.get("depends_on", []),
                        "review_loop": bool(st.get("review_loop", False)),
                    }
                    for i, st in enumerate(subtasks)
                ],
            }
            intake_data = {
                "requirements": task.get("description", ""),
                "approach": "From project plan",
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
                task["title"],
                task.get("description", ""),
                task.get("priority", "medium"),
                task.get("task_type", "general"),
                json.dumps(plan_json),
                json.dumps(intake_data),
            )
            todo_id = str(todo["id"])
            created_ids.append(todo_id)

            # Link the first task to the session
            if session_id and not session_linked:
                await db.execute(
                    "UPDATE todo_items SET chat_session_id = $1 WHERE id = $2",
                    session_id, todo_id,
                )
                await db.execute(
                    "UPDATE project_chat_sessions SET linked_todo_id = $1 WHERE id = $2",
                    todo_id, session_id,
                )
                session_linked = True

            # Insert sub_tasks into the sub_tasks table
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

            # Emit event so orchestrator starts execution immediately
            if event_bus:
                from agents.orchestrator.events import TaskEvent
                try:
                    await event_bus.publish(TaskEvent(
                        event_type="task_created",
                        todo_id=todo_id,
                        state="in_progress",
                    ))
                except Exception:
                    logger.warning("Failed to emit event for plan task %s", todo_id[:8])

        else:
            # No subtasks: create in intake for coordinator to process
            todo = await db.fetchrow(
                """
                INSERT INTO todo_items (project_id, creator_id, title, description, priority, task_type)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
                """,
                project_id,
                user_id,
                task["title"],
                task.get("description", ""),
                task.get("priority", "medium"),
                task.get("task_type", "general"),
            )
            todo_id = str(todo["id"])
            created_ids.append(todo_id)

            # Link the first task to the session
            if session_id and not session_linked:
                await db.execute(
                    "UPDATE todo_items SET chat_session_id = $1 WHERE id = $2",
                    session_id, todo_id,
                )
                await db.execute(
                    "UPDATE project_chat_sessions SET linked_todo_id = $1 WHERE id = $2",
                    todo_id, session_id,
                )
                session_linked = True

            if event_bus:
                from agents.orchestrator.events import TaskEvent
                try:
                    await event_bus.publish(TaskEvent(
                        event_type="task_created",
                        todo_id=todo_id,
                        state="intake",
                    ))
                except Exception:
                    logger.warning("Failed to emit task_created event for %s", todo_id[:8])

    return created_ids


# ── Debug Mode ─────────────────────────────────────────────────────


DEBUG_CHAT_SYSTEM = """\
You are a senior debugging engineer helping with "{project_name}".
{project_context}

You are in an interactive debugging session. Help the user investigate bugs, errors, \
and performance issues by exploring the codebase, checking logs, and querying data.

## How You Work
- Ask clarifying questions about the bug/issue when needed
- Use tools to investigate — read code, search for patterns, run commands, check logs
- Share your findings as you go — don't wait until you have the full picture
- Suggest fixes when you identify the root cause
- Be conversational: explain your reasoning, ask for feedback

{tools_doc}

{debug_context}

Be thorough but conversational. Share findings incrementally."""


async def _generate_debug_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
) -> dict:
    """Generate an AI response in debug mode — interactive debugging assistant."""
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    # Resolve MCP tools for this project
    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )

    # Builtin workspace tools (read_file, list_directory, search_files, run_command)
    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "debugger") if workspace_path else []

    # Build project context
    project_ctx_parts = []
    if project.get("description"):
        project_ctx_parts.append(f"Description: {project['description']}")
    if project.get("repo_url"):
        project_ctx_parts.append(f"Repository: {project['repo_url']}")

    settings = safe_json(project.get("settings_json"))
    debug_context_str = ""
    if settings:
        understanding = settings.get("project_understanding", {})
        if understanding.get("summary"):
            project_ctx_parts.append(f"Summary: {understanding['summary']}")
        if understanding.get("tech_stack"):
            project_ctx_parts.append(f"Tech stack: {', '.join(understanding['tech_stack'])}")

        # Debug context (log sources, MCP hints)
        debug_ctx = settings.get("debug_context", {})
        if debug_ctx:
            debug_parts = []
            for src in debug_ctx.get("log_sources", []):
                desc = src.get("description", "logs available")
                cmd = src.get("log_command", "")
                line = f"  - **{src.get('service_name', 'service')}**: {desc}"
                if cmd:
                    line += f" (command: `{cmd}`)"
                debug_parts.append(line)
            for hint in debug_ctx.get("mcp_hints", []):
                data = ", ".join(hint.get("available_data", []))
                notes = hint.get("notes", "")
                line = f"  - **{hint.get('mcp_server_name', 'MCP')}**: {data}"
                if notes:
                    line += f" — {notes}"
                debug_parts.append(line)
            if debug_parts:
                debug_context_str = "## Debug Context\nAvailable data sources for investigation:\n" + "\n".join(debug_parts)
            if debug_ctx.get("custom_instructions"):
                debug_context_str += f"\n\nCustom debug instructions: {debug_ctx['custom_instructions']}"

    project_context = "\n".join(project_ctx_parts)

    # Build tools documentation
    tools_doc_parts = []
    if builtin_tools:
        tools_doc_parts.append(
            "## Tools Available\n"
            "You have workspace tools to explore the codebase and run commands:\n"
            "- **read_file** — Read a file's contents (path relative to repo root)\n"
            "- **list_directory** — List files and directories\n"
            "- **search_files** — Search for a text pattern across files (grep)\n"
            "- **run_command** — Run a shell command in the repo root (git, builds, tests, logs, etc.)"
        )
    if mcp_tools:
        mcp_names = [t["function"]["name"] for t in mcp_tools if "function" in t]
        if mcp_names:
            tools_doc_parts.append(
                f"You also have MCP tools: {', '.join(mcp_names[:15])}"
            )
    tools_doc = "\n\n".join(tools_doc_parts) if tools_doc_parts else "No workspace tools available."

    system_prompt = DEBUG_CHAT_SYSTEM.format(
        project_name=project["name"],
        project_context=project_context,
        tools_doc=tools_doc,
        debug_context=debug_context_str,
    )

    # Load session history
    history = await db.fetch(
        "SELECT role, content FROM project_chat_messages "
        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 30",
        session_id,
    )
    history = list(reversed(history))

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        messages.append(LLMMessage(role=row["role"], content=row["content"]))
    messages.append(LLMMessage(role="user", content=user_message))

    from agents.providers.base import run_tool_loop

    all_tools = builtin_tools + (mcp_tools or [])
    tools_arg = all_tools if all_tools else None

    mcp_exec = McpToolExecutor(db)

    async def _execute_tool(name: str, arguments: dict) -> str:
        return await mcp_exec.execute_tool(name, arguments, all_tools)

    # Build on_activity callback
    on_activity = None
    if redis and session_id:
        async def on_activity(msg: str) -> None:
            await redis.publish(
                f"chat:session:{session_id}:activity",
                json.dumps({"type": "activity", "activity": msg}),
            )

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override

    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=8,
        on_activity=on_activity,
        **send_kwargs,
    )

    # Store assistant response
    msg_row = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id)
        VALUES ($1, $2, 'assistant', $3, $4) RETURNING *
        """,
        project_id,
        user_id,
        content,
        session_id,
    )
    return dict(msg_row)


# ── Create Task Mode ──────────────────────────────────────────────


CREATE_TASK_SYSTEM = """\
You are a task creation assistant for "{project_name}".
{project_context}

The user wants to create a task. Your job:
1. Parse the user's description into a well-structured task
2. Use action__create_task to create it immediately
3. Include sub_tasks with appropriate agent roles if the scope is clear
4. If the description is too vague, ask ONE clarifying question, then create

Always create exactly ONE task. Be efficient — don't over-discuss, just create.

{tools_doc}

Valid agent roles for sub_tasks: coder, tester, reviewer, pr_creator, report_writer
Valid task types: code, research, document, general
Valid priorities: critical, high, medium, low"""


async def _generate_create_task_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
) -> dict:
    """Generate a response in create-task mode — directly creates tasks from descriptions."""
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    # Only provide the create_task action tool (no delete)
    action_tools = get_actions_as_tools("project")
    # Filter to only create_task
    action_tools = [t for t in (action_tools or []) if "create_task" in t.get("function", {}).get("name", "")]

    # Build project context
    project_ctx_parts = []
    if project.get("description"):
        project_ctx_parts.append(f"Description: {project['description']}")
    if project.get("repo_url"):
        project_ctx_parts.append(f"Repository: {project['repo_url']}")

    settings = safe_json(project.get("settings_json"))
    if settings:
        understanding = settings.get("project_understanding", {})
        if understanding.get("summary"):
            project_ctx_parts.append(f"Summary: {understanding['summary']}")
        if understanding.get("tech_stack"):
            project_ctx_parts.append(f"Tech stack: {', '.join(understanding['tech_stack'])}")

    # Existing tasks for context
    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 10",
        project_id,
    )
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        project_ctx_parts.append("Existing tasks:\n" + "\n".join(task_lines))

    project_context = "\n".join(project_ctx_parts)

    tools_doc = """You have the following action:
- **action__create_task** — Create a new tracked task. Include sub_tasks to send work directly to execution.
  IMPORTANT: Always create ONE task with ALL sub_tasks inside it."""

    system_prompt = CREATE_TASK_SYSTEM.format(
        project_name=project["name"],
        project_context=project_context,
        tools_doc=tools_doc,
    )

    # Load session history
    history = await db.fetch(
        "SELECT role, content FROM project_chat_messages "
        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 20",
        session_id,
    )
    history = list(reversed(history))

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        messages.append(LLMMessage(role=row["role"], content=row["content"]))
    messages.append(LLMMessage(role="user", content=user_message))

    from agents.providers.base import run_tool_loop

    action_context = {
        "db": db,
        "project_id": project_id,
        "user_id": user_id,
        "event_bus": event_bus,
        "session_id": session_id,
        "redis": redis,
    }
    metadata = None

    async def _execute_tool(name: str, arguments: dict) -> str:
        nonlocal metadata
        if is_action_tool(name):
            result_text = await execute_action(name, arguments, action_context)
            try:
                result_data = json.loads(result_text)
                if result_data.get("action") == "task_created":
                    metadata = {
                        "action": result_data["action"],
                        "task_id": result_data.get("task_id"),
                        "task_title": result_data.get("title"),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
            return result_text
        return json.dumps({"error": f"Unknown tool: {name}"})

    on_activity = None
    if redis and session_id:
        async def on_activity(msg: str) -> None:
            await redis.publish(
                f"chat:session:{session_id}:activity",
                json.dumps({"type": "activity", "activity": msg}),
            )

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override

    content, response = await run_tool_loop(
        provider, messages,
        tools=action_tools if action_tools else None,
        tool_executor=_execute_tool,
        max_rounds=3,
        on_activity=on_activity,
        **send_kwargs,
    )

    # Store assistant response
    msg_row = await db.fetchrow(
        """
        INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
        VALUES ($1, $2, 'assistant', $3, $4::jsonb, $5) RETURNING *
        """,
        project_id,
        user_id,
        content,
        json.dumps(metadata) if metadata else None,
        session_id,
    )
    return dict(msg_row)
