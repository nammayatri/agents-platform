"""LLM response generators for project chat modes.

Extracted from project_chat.py to keep route handlers thin.
Each function generates an AI response for a specific chat mode
(chat, plan, debug, create_task).
"""

import json
import logging

from agents.api.chat_actions import execute_action, get_actions_as_tools, is_action_tool
from agents.utils.json_helpers import safe_json

logger = logging.getLogger(__name__)


# ── Shared Helpers ───────────────────────────────────────────────────


async def resolve_planner_config(db, user_id: str) -> dict | None:
    """Look up a custom planner agent config for the user."""
    row = await db.fetchrow(
        "SELECT * FROM agent_configs WHERE role = 'planner' AND owner_id = $1 AND is_active = TRUE "
        "ORDER BY updated_at DESC LIMIT 1",
        user_id,
    )
    return dict(row) if row else None


def is_plan_acceptance(message: str) -> bool:
    """Check if the user message is accepting the proposed plan."""
    lower = message.lower().strip()
    accept_phrases = [
        "looks good", "approve", "accept", "go ahead", "lgtm",
        "ship it", "proceed", "let's do it", "start", "yes",
        "perfect", "do it", "execute", "run it",
    ]
    return any(phrase in lower for phrase in accept_phrases)


def _build_project_context(project: dict, settings: dict | None) -> str:
    """Build a project context string from settings/understanding data."""
    parts: list[str] = []
    if not settings:
        return ""

    understanding = settings.get("project_understanding", {})
    if understanding:
        if understanding.get("summary"):
            parts.append(f"Summary: {understanding['summary']}")
        if understanding.get("tech_stack"):
            parts.append(f"Tech stack: {', '.join(understanding['tech_stack'])}")

        cross_links = understanding.get("cross_repo_links", [])
        if cross_links:
            link_lines = []
            for link in cross_links:
                dep = link.get("dep_name", "?")
                pattern = link.get("integration_pattern", "")
                main_files = ", ".join(link.get("main_repo_files", [])[:5])
                line = f"  - {dep}: {pattern}"
                if main_files:
                    line += f" (files: {main_files})"
                link_lines.append(line)
            parts.append("Cross-repo integration points:\n" + "\n".join(link_lines))

        dep_map = understanding.get("dependency_map", [])
        if dep_map:
            dep_lines = []
            for dm in dep_map:
                line = f"  - {dm.get('name', '?')}: {dm.get('role', '?')}"
                int_files = dm.get("integration_files", [])
                if int_files:
                    line += f" (integration files: {', '.join(int_files[:3])})"
                dep_lines.append(line)
            parts.append("Dependency roles:\n" + "\n".join(dep_lines))

    work_rules = settings.get("work_rules", {})
    if work_rules:
        rules_parts = []
        for cat, items in work_rules.items():
            if items:
                rules_parts.append(f"  {cat}: {', '.join(items)}")
        if rules_parts:
            parts.append("Current work rules:\n" + "\n".join(rules_parts))

    debug_ctx = settings.get("debug_context", {})
    if debug_ctx:
        debug_parts = []
        for src in debug_ctx.get("log_sources", []):
            debug_parts.append(f"  - {src.get('service_name', 'service')}: {src.get('description', 'logs available')}")
        for hint in debug_ctx.get("mcp_hints", []):
            data = ", ".join(hint.get("available_data", []))
            debug_parts.append(f"  - {hint.get('mcp_server_name', 'MCP')}: {data}")
        if debug_parts:
            parts.append("Debug context (use debugger agent for bug investigations):\n" + "\n".join(debug_parts))

    return "\n\n".join(parts)


async def _build_on_activity(redis, session_id: str | None):
    """Build an on_activity callback for streaming tool updates."""
    if not redis or not session_id:
        return None

    async def on_activity(msg: str) -> None:
        await redis.publish(
            f"chat:session:{session_id}:activity",
            json.dumps({"type": "activity", "activity": msg}),
        )

    return on_activity


# ── Chat Mode (default) ─────────────────────────────────────────────


async def generate_project_response(
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

    planner_config = await resolve_planner_config(db, user_id)

    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )
    skills_ctx = await tools_reg.build_skills_context(
        project_id=project_id, user_id=user_id,
    )

    linked_todo_id = None
    if session_id:
        session_row = await db.fetchrow(
            "SELECT linked_todo_id FROM project_chat_sessions WHERE id = $1", session_id,
        )
        if session_row and session_row["linked_todo_id"]:
            linked_todo_id = str(session_row["linked_todo_id"])

    if linked_todo_id:
        action_tools = get_actions_as_tools("session_task")
    else:
        action_tools = get_actions_as_tools("project")

    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "planner") if workspace_path else []

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

    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 20",
        project_id,
    )
    tasks_ctx = ""
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        tasks_ctx = "\n\nExisting tasks:\n" + "\n".join(task_lines)

    settings = safe_json(project.get("settings_json"))
    project_ctx = ""
    if settings:
        project_ctx = "\n" + _build_project_context(project, settings)

    if planner_config and planner_config.get("system_prompt"):
        base_prompt = planner_config["system_prompt"]
    else:
        from agents.agents.registry import get_default_system_prompt
        base_prompt = get_default_system_prompt("planner")

    if linked_todo_id:
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
- **read_file** — Read a file's contents (path relative to repo root, or ../deps/{name}/path for dependency repos)
- **list_directory** — List files and directories (path relative to repo root, ../deps/ for dependency repos)
- **search_files** — Search for a text pattern across files (grep). Use path="../deps/{name}/" to search deps.
- **run_command** — Run a shell command in the repo root. Use for git, gh (GitHub CLI), builds, tests, etc.
  Examples: `git log --oneline -10`, `git status`, `gh issue list`, `gh pr list`, `gh pr view 42`

IMPORTANT: Always explore the codebase with these tools before creating tasks. \
Read relevant files, understand the existing code structure, and then plan accordingly. \
Do NOT guess or assume what the codebase looks like — read it first. \
Use run_command with git/gh to check repo status, open PRs, issues, and branches.

CROSS-REPO: Dependency repos are available at ../deps/{name}/ (read-only). \
Use list_directory("../deps/") to see them. When tasks involve cross-repo concerns, \
explore both main repo AND deps before planning. Enrich user queries with specific \
file paths and current patterns discovered during exploration."""
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

    all_tools = (action_tools or []) + builtin_tools + (mcp_tools or [])
    tools_arg = all_tools if all_tools else None

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

    on_activity = await _build_on_activity(redis, session_id)

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


# ── Plan Mode ────────────────────────────────────────────────────────

# Read-only builtins allowed in plan mode (no write_file, edit_file, run_command)
PLAN_MODE_BUILTIN_TOOLS = {"read_file", "list_directory", "search_files", "semantic_search"}


def _filter_plan_mode_tools(
    builtin_tools: list[dict],
    mcp_tools: list[dict] | None,
    planner_config: dict | None,
) -> list[dict]:
    """Filter tools for plan mode: read-only builtins + all MCP tools.

    If planner_config has tools_enabled set, further restrict builtins
    to only those in the allowed list.  MCP tools pass through unfiltered
    (controlled at the project-level MCP server enable/disable).
    """
    filtered = [t for t in builtin_tools if t["name"] in PLAN_MODE_BUILTIN_TOOLS]

    # Honour tools_enabled from agent config if set
    if planner_config:
        raw = planner_config.get("tools_enabled")
        if raw and len(raw) > 0:
            allowed = set(raw)
            filtered = [t for t in filtered if t["name"] in allowed]

    return filtered + (mcp_tools or [])


def _compact_chat_history(
    history: list[dict],
    max_tokens: int,
    model: str,
) -> list[dict]:
    """Compact old chat history to fit within a token budget.

    Keeps the most recent messages in full.  For older messages, truncates
    long content (especially assistant messages with code blocks or plan JSON)
    using a middle strategy that preserves start + end.
    """
    from agents.utils.context_budget import truncate_to_budget
    from agents.utils.token_counter import count_tokens

    if not history:
        return history

    total_tokens = sum(count_tokens(h["content"] or "", model) for h in history)
    if total_tokens <= max_tokens:
        return list(history)

    # Keep last 6 messages (3 exchanges) in full
    keep_recent = min(6, len(history))
    recent = history[-keep_recent:]
    older = history[:-keep_recent]

    recent_tokens = sum(count_tokens(h["content"] or "", model) for h in recent)
    remaining_budget = max(max_tokens - recent_tokens, 2000)

    if not older:
        return list(recent)

    per_msg_budget = remaining_budget // len(older)

    compacted: list[dict] = []
    for msg in older:
        content = msg["content"] or ""
        msg_tokens = count_tokens(content, model)
        if msg_tokens > per_msg_budget:
            truncated = truncate_to_budget(content, per_msg_budget, model, strategy="middle")
            compacted.append({"role": msg["role"], "content": truncated})
        else:
            compacted.append(msg)

    return compacted + list(recent)


PLAN_MODE_SYSTEM = """\
You are a project planning assistant for "{project_name}".
{project_context}

You are in PLANNING MODE. Your job is to:
1. Explore the codebase to understand current structure and patterns
2. Discuss the project scope, requirements, and technical approach with the user
3. Ask clarifying questions to understand the full picture
4. Progressively build a structured plan with subtasks

{tools_doc}

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


async def generate_plan_response(
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
    """Generate an AI response in plan mode.

    Uses a tool loop with read-only workspace tools + MCP tools so the
    planner can explore the codebase before proposing a plan.  Write tools
    (write_file, edit_file, run_command) are excluded.
    """
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.base import run_tool_loop
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    planner_config = await resolve_planner_config(db, user_id)

    # ── Resolve tools ────────────────────────────────────────────────
    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )
    skills_ctx = await tools_reg.build_skills_context(
        project_id=project_id, user_id=user_id,
    )

    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "planner") if workspace_path else []

    # Filter to read-only tools; respect tools_enabled from agent config
    all_tools = _filter_plan_mode_tools(builtin_tools, mcp_tools, planner_config)
    tools_arg = all_tools if all_tools else None

    # ── Build project context ────────────────────────────────────────
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

    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 20",
        project_id,
    )
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        project_ctx_parts.append("Existing tasks:\n" + "\n".join(task_lines))

    project_context = "\n".join(project_ctx_parts)

    # ── Build tools documentation for system prompt ──────────────────
    tools_doc_parts: list[str] = []
    has_builtins = any(t.get("_builtin") for t in all_tools) if all_tools else False
    if has_builtins:
        tools_doc_parts.append(
            "## Workspace Tools (Read-Only)\n"
            "You have read-only tools to explore the codebase — USE THEM to research before planning:\n"
            "- **read_file** — Read a file's contents (path relative to repo root, or ../deps/{name}/path for deps)\n"
            "- **list_directory** — List files and directories\n"
            "- **search_files** — Search for a text pattern across files (grep)\n"
            "- **semantic_search** — Search the codebase semantically with natural language\n\n"
            "IMPORTANT: Always explore the codebase with these tools before proposing a plan.\n"
            "Read relevant files, understand the existing code structure, and plan accordingly.\n"
            "Do NOT guess what the codebase looks like — read it first.\n\n"
            "CROSS-REPO: Dependency repos are available at ../deps/{name}/ (read-only).\n"
            "Use list_directory(\"../deps/\") to see them.\n\n"
            "NOTE: You are in PLANNING mode — you cannot write files or run commands.\n"
            "Your job is to research and plan, not implement."
        )
    mcp_only = [t for t in (all_tools or []) if not t.get("_builtin")]
    if mcp_only:
        mcp_names = [t.get("name", "") for t in mcp_only if t.get("name")]
        if mcp_names:
            tools_doc_parts.append(
                f"You also have MCP tools available: {', '.join(mcp_names[:15])}"
            )
    if not tools_doc_parts:
        tools_doc_parts.append(
            "No workspace tools are available. Plan based on the information provided."
        )
    tools_doc = "\n\n".join(tools_doc_parts)

    system_prompt = PLAN_MODE_SYSTEM.format(
        project_name=project["name"],
        project_context=project_context,
        tools_doc=tools_doc,
    )

    if skills_ctx:
        system_prompt += skills_ctx

    # ── Load & compact chat history ──────────────────────────────────
    history = await db.fetch(
        "SELECT role, content FROM project_chat_messages "
        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 40",
        session_id,
    )
    history = list(reversed(history))

    # Determine model for token counting
    plan_send_kwargs: dict = {}
    if model_override:
        plan_send_kwargs["model"] = model_override
    elif planner_config and planner_config.get("model_preference"):
        plan_send_kwargs["model"] = planner_config["model_preference"]

    effective_model = plan_send_kwargs.get("model") or provider.default_model
    history = _compact_chat_history(history, max_tokens=40_000, model=effective_model)

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        messages.append(LLMMessage(role=row["role"], content=row["content"]))
    messages.append(LLMMessage(role="user", content=user_message))

    # ── Tool executor ────────────────────────────────────────────────
    mcp_exec = McpToolExecutor(db)

    async def _execute_tool(name: str, arguments: dict) -> str:
        return await mcp_exec.execute_tool(name, arguments, all_tools)

    on_activity = await _build_on_activity(redis, session_id)

    # ── Run tool loop ────────────────────────────────────────────────
    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=5,
        on_activity=on_activity,
        **plan_send_kwargs,
    )

    # ── Plan extraction & acceptance (unchanged) ─────────────────────
    metadata = None
    if "```json" in content and '"action"' in content and '"create_plan"' in content:
        try:
            # Use rindex to find the *last* JSON block (avoids false matches
            # from tool results that might contain markdown JSON fences).
            json_start = content.rindex("```json") + 7
            json_end = content.index("```", json_start)
            plan_data = json.loads(content[json_start:json_end].strip())

            if plan_data.get("action") == "create_plan":
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

    plan_json = session.get("plan_json")
    if plan_json and is_plan_acceptance(user_message):
        plan_json = safe_json(plan_json) if isinstance(plan_json, str) else plan_json

        created_tasks = await create_tasks_from_plan(
            project_id=project_id,
            user_id=user_id,
            plan=plan_json,
            db=db,
            event_bus=event_bus,
            session_id=session_id,
        )

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

        task_summary = "\n".join(f"  - {t['title']}" for t in plan_json.get("tasks", []))
        content = (
            f"{content}\n\n"
            f"**Plan accepted!** Created {len(created_tasks)} tasks:\n{task_summary}"
        )

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


# ── Task Creation from Plan ──────────────────────────────────────────


async def create_tasks_from_plan(
    *, project_id: str, user_id: str, plan: dict, db, event_bus=None, session_id: str | None = None,
) -> list[str]:
    """Create todo items from a plan.

    When subtasks are defined, creates the task directly in 'in_progress'
    with sub_tasks inserted into the DB.
    """
    created_ids = []
    session_linked = False

    for task in plan.get("tasks", []):
        subtasks = task.get("subtasks", [])

        if subtasks:
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

                if review_loop:
                    await db.execute(
                        "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                        row["id"],
                    )

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


# ── Debug Mode ───────────────────────────────────────────────────────


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


async def generate_debug_response(
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
    """Generate an AI response in debug mode."""
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )

    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "debugger") if workspace_path else []

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

    on_activity = await _build_on_activity(redis, session_id)

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


# ── Create Task Mode ────────────────────────────────────────────────


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


async def generate_create_task_response(
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
    """Generate a response in create-task mode."""
    from agents.providers.registry import ProviderRegistry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = ProviderRegistry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    action_tools = get_actions_as_tools("project")
    action_tools = [t for t in (action_tools or []) if "create_task" in t.get("function", {}).get("name", "")]

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

    on_activity = await _build_on_activity(redis, session_id)

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
