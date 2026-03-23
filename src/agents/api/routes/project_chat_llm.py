"""LLM response generators for project chat modes.

Extracted from project_chat.py to keep route handlers thin.
Each function generates an AI response for a specific chat mode
(chat, plan, debug, create_task).
"""

import gc
import json
import logging

from agents.api.chat_actions import execute_action, get_actions_as_tools, is_action_tool
from agents.utils.json_helpers import safe_json
from agents.utils.repo_utils import resolve_target_repo

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


async def _build_on_token(redis, session_id: str | None):
    """Build an on_token callback for streaming LLM text deltas.

    Buffers tokens and publishes in batches (every 15+ chars) to avoid
    flooding Redis with tiny per-token messages.
    """
    if not redis or not session_id:
        return None

    channel = f"chat:session:{session_id}:activity"
    _buf: list[str] = []
    _buf_len = 0

    async def on_token(delta: str) -> None:
        nonlocal _buf_len
        _buf.append(delta)
        _buf_len += len(delta)
        if _buf_len >= 15:
            text = "".join(_buf)
            _buf.clear()
            _buf_len = 0
            await redis.publish(channel, json.dumps({"type": "token", "token": text}))

    async def flush() -> None:
        nonlocal _buf_len
        if _buf:
            text = "".join(_buf)
            _buf.clear()
            _buf_len = 0
            await redis.publish(channel, json.dumps({"type": "token", "token": text}))
        await redis.publish(channel, json.dumps({"type": "token_done"}))

    on_token.flush = flush  # type: ignore[attr-defined]
    return on_token


SUMMARIZE_SYSTEM = """\
You are summarizing the results of a research/exploration session.
An AI agent was asked a question and used tools (reading files, searching code, running commands) to investigate.
Below is the agent's raw working output — it contains useful findings mixed with narration like "Let me check…" or "I'll look at…".

Your job: produce a **clean, direct response** that answers the user's original question.

Rules:
- Lead with the answer or key findings — no preamble.
- Preserve file paths, code snippets, line numbers, and specific technical details.
- Use markdown for structure (headings, bullets, code blocks) when helpful.
- Do NOT include phrases like "Let me…", "I'll…", "Now searching…", "Based on my analysis…" — just state the facts.
- If the agent discovered multiple relevant things, organize them clearly.
- Keep the response concise but complete — don't drop important details."""


async def _summarize_findings(
    provider,
    user_message: str,
    raw_content: str,
    on_activity=None,
    **send_kwargs,
) -> str:
    """Distill the raw tool-loop output into a clean response for the user."""
    from agents.schemas.agent import LLMMessage

    if on_activity:
        await on_activity("Summarizing findings...")

    # Truncate raw content to avoid blowing up context on very long explorations
    truncated = raw_content[:24000] if len(raw_content) > 24000 else raw_content

    messages = [
        LLMMessage(role="system", content=SUMMARIZE_SYSTEM),
        LLMMessage(
            role="user",
            content=f"**User's question:**\n{user_message}\n\n**Agent's raw output:**\n{truncated}",
        ),
    ]

    # Remove tool-related kwargs; this is a plain text completion
    clean_kwargs = {k: v for k, v in send_kwargs.items() if k in ("model",)}
    try:
        resp = await provider.send_message(messages, max_tokens=8192, temperature=0.2, **clean_kwargs)
        return resp.content or raw_content
    except Exception:
        logger.warning("Summarization call failed, returning raw content", exc_info=True)
        return raw_content


def _build_execution_metadata(tool_events: list[dict], response) -> dict:
    """Build a compact execution summary from tool loop events for the UI."""
    tool_calls = []
    for e in tool_events:
        if e.get("type") == "tool_result" and not e.get("error"):
            tool_calls.append({
                "name": e.get("name", ""),
                "result_preview": (e.get("result_preview", "") or "")[:200],
            })
    rounds = max(
        (e.get("round", 0) for e in tool_events if e.get("type") == "llm_thinking"),
        default=0,
    )
    total_tokens_in = sum(
        e.get("tokens_in", 0) for e in tool_events if e.get("type") == "llm_thinking"
    )
    total_tokens_out = sum(
        e.get("tokens_out", 0) for e in tool_events if e.get("type") == "llm_thinking"
    )
    return {
        "tool_calls": tool_calls,
        "rounds": rounds,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "model": getattr(response, "model", ""),
        "stop_reason": getattr(response, "stop_reason", ""),
    }


# ── Intent Detection ────────────────────────────────────────────────

import re as _re

# Keyword patterns for instant classification (no LLM call needed).
# Checked in order — first match wins.
_KEYWORD_RULES: list[tuple[str, "_re.Pattern[str]"]] = [
    ("create_task", _re.compile(
        r"\b(create|add|make|new|open)\b.{0,15}\b(task|ticket|issue|todo|work\s*item)\b",
        _re.IGNORECASE,
    )),
    ("debug", _re.compile(
        r"\b(debug|bug|error|crash|broken|failing|exception|traceback|stack\s*trace|why\s+is.*(?:broken|failing|wrong|slow))\b",
        _re.IGNORECASE,
    )),
    # Only match "plan" when it's an action request, not a reference to
    # an existing plan (e.g. "the plan wasn't right").  Require either:
    #   - a verb+plan combo: "let's plan", "create a plan", "make a plan", "build a plan", "need a plan"
    #   - plan as a verb: "plan this", "plan the", "plan for", "plan out"
    #   - dedicated planning words: architect, roadmap, strategy
    ("plan", _re.compile(
        r"(?:"
        r"let'?s\s+plan"
        r"|(?:create|make|build|need|want|write)\s+(?:a\s+)?plan"
        r"|\bplan\s+(?:this|the|for|out|how)\b"
        r"|\b(architect|roadmap|strategy|how\s+should\s+we\s+build)\b"
        r")",
        _re.IGNORECASE,
    )),
]

_VALID_MODES = {"chat", "plan", "debug", "create_task"}

# Minimal prompt — ask for a single word, no JSON overhead.
_INTENT_PROMPT = """\
Classify into: chat, plan, debug, create_task. Current: {current_mode}.
Reply with ONE word only."""


def _keyword_classify(message: str) -> str | None:
    """Try to classify the message using keyword patterns. Returns mode or None."""
    for mode, pattern in _KEYWORD_RULES:
        if pattern.search(message):
            return mode
    return None


async def detect_intent(
    *,
    provider,
    user_message: str,
    current_routing_mode: str,
    recent_messages: list[dict],
) -> tuple[str, float]:
    """Detect the intent of a user message.

    Fast path: keyword patterns resolve instantly (no LLM call).
    Slow path: one fast-model LLM call for ambiguous messages.

    Returns (detected_mode, confidence).
    """
    # ── Fast path: keyword match ──────────────────────────────────
    kw_mode = _keyword_classify(user_message)
    if kw_mode:
        logger.info("detect_intent: keyword match → %s", kw_mode)
        return kw_mode, 1.0

    # ── Fast path: short follow-up keeps current mode ─────────────
    # Messages under 40 chars with no mode-switching signals are
    # almost always continuations ("yes", "continue", "ok do it").
    if len(user_message) < 40 and current_routing_mode in _VALID_MODES:
        return current_routing_mode, 0.8

    # ── Slow path: LLM classification ─────────────────────────────
    from agents.schemas.agent import LLMMessage

    prompt = _INTENT_PROMPT.format(current_mode=current_routing_mode)

    fast_model = provider.fast_model or provider.default_model
    try:
        response = await provider.send_message(
            [LLMMessage(role="user", content=user_message)],
            model=fast_model,
            max_tokens=10,
            temperature=0.0,
            system_prompt=prompt,
        )
    except Exception as exc:
        logger.warning("detect_intent: LLM call failed (%s), keeping %s", exc, current_routing_mode)
        return current_routing_mode, 0.0

    try:
        raw = response.content.strip().lower().strip('"').strip("'").strip()
        # Handle JSON responses: {"mode": "chat"} or just "chat"
        if raw.startswith("{"):
            data = json.loads(raw)
            mode = str(data.get("mode", current_routing_mode)).lower().strip()
        else:
            # Plain word response — take the first token
            mode = raw.split()[0] if raw else current_routing_mode

        if mode in _VALID_MODES:
            return mode, 0.7
        logger.warning("detect_intent: invalid mode %r, keeping %s", mode, current_routing_mode)
        return current_routing_mode, 0.0
    except Exception:
        logger.debug("detect_intent: parse failed: %s", response.content[:100])
        return current_routing_mode, 0.0


# ── Chat Mode (default) ─────────────────────────────────────────────


async def generate_project_response(
    *,
    project_id: str,
    session_id: str | None,
    user_id: str,
    user_message: str,
    user_display_name: str = "",
    intent: str | None,
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
    placeholder_id: str | None = None,
) -> dict:
    """Generate an AI response for a project-level chat message."""
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = get_registry(db)
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

    # Load configured repos (main + deps) so LLM knows which repos are available
    context_docs = project.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)
    configured_dep_names = [
        d["name"] for d in context_docs
        if isinstance(d, dict) and d.get("name") and d.get("repo_url")
    ]

    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "planner") if workspace_path else []

    if session_id:
        history = await db.fetch(
            "SELECT m.role, m.content, u.display_name AS sender_name "
            "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
            "WHERE m.session_id = $1 ORDER BY m.created_at DESC LIMIT 30",
            session_id,
        )
    else:
        history = await db.fetch(
            "SELECT m.role, m.content, u.display_name AS sender_name "
            "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
            "WHERE m.project_id = $1 AND m.user_id = $2 AND m.session_id IS NULL "
            "ORDER BY m.created_at DESC LIMIT 30",
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
        # Build repo context for task creation
        repo_context = "\nMain repository: " + (project.get("repo_url") or "(not configured)")
        if configured_dep_names:
            dep_str = ", ".join(f"'{n}'" for n in configured_dep_names)
            repo_context += f"\nConfigured dependency repos: {dep_str}"
            repo_context += (
                "\nWhen creating sub_tasks, set target_repo to 'main' for main repo work, "
                "or to the EXACT dependency name for work in a dependency repo."
            )
        else:
            repo_context += "\nNo dependency repos configured — use target_repo='main' for all sub_tasks."

        tools_doc = f"""
You have the following actions available:
- **action__create_task** — Create a new tracked task. Use when the user describes work they want done. \
You can include sub_tasks to send work directly to execution, or omit them for the intake/planning pipeline. \
IMPORTANT: Always create ONE task with ALL sub_tasks inside it. Never create multiple separate tasks for related work. \
Every sub_task MUST have target_repo set — this is required.
- **action__delete_task** — Delete a task. ALWAYS ask for user confirmation before calling this.
{repo_context}"""

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

    system_prompt += "\n\nMessages from different users are prefixed with [Name]. Address them by name when relevant."

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        content = row["content"]
        if row["role"] == "user" and row.get("sender_name"):
            content = f"[{row['sender_name']}]: {content}"
        messages.append(LLMMessage(role=row["role"], content=content))
    # Current message with user identity
    current_content = f"[{user_display_name}]: {user_message}" if user_display_name else user_message
    messages.append(LLMMessage(role="user", content=current_content))

    logger.info(
        "[chat] session=%s mode=%s dep_repos=%s system_prompt_tail:\n...%s",
        session_id, intent,
        configured_dep_names if configured_dep_names else "(none)",
        system_prompt[-800:],
    )

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
            logger.info(
                "[chat_tool] session=%s tool=%s raw_arguments:\n%s",
                session_id, name,
                json.dumps(arguments, indent=2, default=str)[:5000],
            )
            result_text = await execute_action(name, arguments, action_context)
            try:
                result_data = json.loads(result_text)
                if result_data.get("action") in ("task_created", "task_deleted"):
                    metadata = {
                        "action": result_data["action"],
                        "task_id": result_data.get("task_id"),
                        "task_title": result_data.get("title"),
                    }
                    # When the task was created in plan_ready (awaiting user
                    # approval), surface the plan in chat for approve/reject.
                    if result_data.get("awaiting_approval") and result_data.get("plan_data"):
                        metadata["action"] = "task_plan_ready"
                        metadata["plan_data"] = result_data["plan_data"]
                elif result_data.get("action") == "task_plan_ready":
                    # Deferred creation: plan stored in session, no task_id yet
                    metadata = {
                        "action": "task_plan_ready",
                        "task_title": result_data.get("title"),
                        "plan_data": result_data.get("plan_data"),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
            return result_text
        return await mcp_exec.execute_tool(name, arguments, builtin_tools + (mcp_tools or []))

    on_activity = await _build_on_activity(redis, session_id)
    on_token = await _build_on_token(redis, session_id)

    # Allow users to inject guidance while the chat agent explores
    _inject_key = f"chat:session:{session_id}:inject" if session_id else None

    async def _check_inject() -> str | None:
        if redis and _inject_key:
            return await redis.lpop(_inject_key)
        return None

    # Collect tool execution events for UI visibility
    tool_events: list[dict] = []

    async def _on_tool_event(event: dict) -> None:
        # Cap tool_events to prevent unbounded memory growth during long loops.
        # Keep all llm_thinking events (small, needed for token stats) but
        # trim tool_result/tool_start entries to the most recent 200.
        if len(tool_events) > 500:
            # Keep llm_thinking events + last 200 others
            thinking = [e for e in tool_events if e.get("type") == "llm_thinking"]
            others = [e for e in tool_events if e.get("type") != "llm_thinking"]
            tool_events[:] = thinking + others[-200:]
        tool_events.append(event)

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override
    elif planner_config and planner_config.get("model_preference"):
        send_kwargs["model"] = planner_config["model_preference"]

    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=70,
        nudge_tools=False,
        on_activity=on_activity,
        on_tool_event=_on_tool_event,
        on_inject_check=_check_inject,
        on_token=on_token,
        **send_kwargs,
    )

    # Flush remaining buffered tokens
    if on_token and hasattr(on_token, "flush"):
        await on_token.flush()

    # Guard against empty responses
    if not content or not content.strip():
        logger.warning("[chat] empty response, retrying without streaming session=%s", session_id)
        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=_execute_tool,
            max_rounds=70,
            nudge_tools=False,
            on_activity=on_activity,
            on_tool_event=_on_tool_event,
            **send_kwargs,
        )
    if not content or not content.strip():
        content = "I wasn't able to generate a response. Could you rephrase your request?"

    if redis and _inject_key:
        while await redis.lpop(_inject_key):
            pass

    # Summarize findings when the agent explored extensively.
    # Skip for short interactions (< 3 tool rounds or short output) to
    # avoid an extra LLM call that adds latency without much value.
    rounds = sum(1 for e in tool_events if e.get("type") == "llm_thinking")
    raw_output = None
    if rounds >= 3 and content and len(content) > 4000:
        raw_output = content
        content = await _summarize_findings(
            provider, user_message, content,
            on_activity=on_activity,
            **send_kwargs,
        )

    # Merge execution metadata into message metadata
    execution_meta = _build_execution_metadata(tool_events, response)
    if metadata is None:
        metadata = {}
    metadata["execution"] = execution_meta
    if raw_output:
        metadata["raw_output"] = raw_output[:20000]

    if placeholder_id:
        msg_row = await db.fetchrow(
            """
            UPDATE project_chat_messages
            SET content = $2, metadata_json = $3, updated_at = NOW()
            WHERE id = $1 RETURNING *
            """,
            placeholder_id, content, metadata,
        )
    else:
        msg_row = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
            VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
            """,
            project_id, user_id, content, metadata, session_id,
        )
    # Free large locals and nudge GC after potentially heavy tool loops
    tool_events.clear()
    result = dict(msg_row)
    del msg_row, content, metadata, response
    gc.collect()
    return result


# ── Plan Mode ────────────────────────────────────────────────────────

# Plan mode builtins: read-only + run_command for git/grep/build (no write_file, edit_file)
PLAN_MODE_BUILTIN_TOOLS = {"read_file", "list_directory", "search_files", "semantic_search", "run_command"}


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

{tools_doc}

YOUR PROCESS — follow this order:
1. UNDERSTAND THE REQUEST — Read the user's message carefully. Identify what they want to achieve.
2. GATHER MISSING INFORMATION — If the request is vague or high-level:
   - Ask 1-2 targeted clarifying questions about scope, priorities, or constraints
   - Use tools to explore relevant code, understand the current state, find patterns
   - Do NOT propose a plan until you understand the problem well enough to define concrete subtasks
3. PROPOSE A PLAN — Only after you have sufficient context, output the plan JSON.
4. ITERATE — If the user requests changes, revise and re-propose.

RESPONSE STYLE — MANDATORY:
- Keep responses SHORT. Max 2-3 short paragraphs for discussion, or a bullet list.
- Use bullet points, not long prose. No preamble, no summaries of what you already know.
- Never restate the user's question. Never recap project context back to the user.
- When asked to find a bug or investigate: go read the actual code with tools, then report findings as a short bullet list.
- When proposing a plan: just output the JSON block with minimal preamble (1 sentence max).

When agreed on a plan, output JSON:
```json
{{
  "action": "create_plan",
  "plan_title": "...",
  "tasks": [{{
    "title": "...",
    "description": "...",
    "priority": "medium",
    "task_type": "code",
    "subtasks": [
      {{"title": "...", "description": "...", "agent_role": "coder", "depends_on": [], "parallel": false, "target_repo": "main"}}
    ]
  }}]
}}
```

Rules: ONE task, all work as subtasks. depends_on = 0-based indexes. parallel: true for concurrent work. \
Roles: coder, tester, reviewer, debugger, pr_creator, report_writer. \
Types: code, research, document, general. Priorities: critical, high, medium, low. \
target_repo: REQUIRED on every subtask — "main" for main repo, or the exact dependency name for dep work.
"""


async def generate_plan_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    user_display_name: str = "",
    project: dict,
    session: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
    placeholder_id: str | None = None,
) -> dict:
    """Generate an AI response in plan mode.

    Uses a tool loop with read-only workspace tools + MCP tools so the
    planner can explore the codebase before proposing a plan.  Write tools
    (write_file, edit_file, run_command) are excluded.
    """
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.base import run_tool_loop
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = get_registry(db)
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

    # Inject index dirs for semantic_search (main + deps)
    if workspace_path and builtin_tools:
        import os
        _plan_idx = os.path.join(workspace_path, ".agent_index")
        _deps_idx_root = os.path.join(workspace_path, ".agent_index_deps")
        _dep_idx_dirs: dict[str, str] = {}
        if os.path.isdir(_deps_idx_root):
            try:
                for entry in os.listdir(_deps_idx_root):
                    idx_dir = os.path.join(_deps_idx_root, entry)
                    if os.path.isdir(idx_dir):
                        _dep_idx_dirs[entry] = idx_dir
            except OSError:
                pass
        for bt in builtin_tools:
            if bt["name"] == "semantic_search":
                bt["_index_dir"] = _plan_idx
                if _dep_idx_dirs:
                    bt["_dep_index_dirs"] = _dep_idx_dirs

    # Filter to read-only tools; respect tools_enabled from agent config
    all_tools = _filter_plan_mode_tools(builtin_tools, mcp_tools, planner_config)
    tools_arg = all_tools if all_tools else None

    # ── Build project context ────────────────────────────────────────
    project_ctx_parts = []
    if project.get("description"):
        project_ctx_parts.append(f"Description: {project['description']}")
    if project.get("repo_url"):
        project_ctx_parts.append(f"Repository: {project['repo_url']}")

    # Add explicit list of dependency repos for target_repo routing
    context_docs = project.get("context_docs") or []
    if isinstance(context_docs, str):
        import json as _json
        context_docs = _json.loads(context_docs)
    dep_repo_names = [d["name"] for d in context_docs if isinstance(d, dict) and d.get("name") and d.get("repo_url")]
    if dep_repo_names:
        dep_str = ", ".join(f"'{n}'" for n in dep_repo_names)
        project_ctx_parts.append(
            f"Dependency repos (for target_repo field): {dep_str}. "
            "Set target_repo to the EXACT dependency name for subtasks that modify a dep repo. "
            "Tool outputs include [Repository: <name>] labels — use these to determine which repo each file belongs to."
        )

    settings = safe_json(project.get("settings_json"))

    # Reuse the shared context builder (includes full understanding,
    # cross-repo links, dependency map, work rules, debug context)
    if settings:
        shared_ctx = _build_project_context(project, settings)
        if shared_ctx:
            project_ctx_parts.append(shared_ctx)

        # Per-dependency understandings — condensed
        dep_understandings = settings.get("dep_understandings", {})
        if dep_understandings and isinstance(dep_understandings, dict):
            dep_ctx_lines = []
            for dep_name, dep_u in dep_understandings.items():
                if not isinstance(dep_u, dict):
                    continue
                purpose = dep_u.get("purpose", "") or dep_u.get("summary", "")
                dep_ctx_lines.append(f"  - {dep_name}: {purpose[:150]}")
            if dep_ctx_lines:
                project_ctx_parts.append(
                    "Dependencies:\n" + "\n".join(dep_ctx_lines)
                )

        # Cross-repo linking — just the overview
        linking_doc = settings.get("linking_document", {})
        if linking_doc and isinstance(linking_doc, dict):
            overview = linking_doc.get("overview", "")
            if overview:
                project_ctx_parts.append(
                    f"Cross-repo:\n{overview[:500]}"
                )

    # Build repo map if workspace exists — gives structural awareness
    # so the planner doesn't need to list_directory constantly
    if workspace_path:
        import os
        repo_dir = os.path.join(workspace_path, "repo")
        if os.path.isdir(repo_dir):
            try:
                from agents.indexing import build_indexes_and_repo_map
                index_dir = os.path.join(workspace_path, ".agent_index")
                repo_map_path = os.path.join(index_dir, "repo_map.txt")
                if os.path.isfile(repo_map_path):
                    with open(repo_map_path, "r") as f:
                        repo_map = f.read()
                    if repo_map:
                        if len(repo_map) > 3000:
                            repo_map = repo_map[:3000] + "\n..."
                        project_ctx_parts.append(
                            f"Repo map:\n{repo_map}"
                        )
            except Exception:
                pass

    todos = await db.fetch(
        "SELECT title, state, priority, task_type FROM todo_items "
        "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 20",
        project_id,
    )
    if todos:
        task_lines = [f"  - [{t['state']}] ({t['priority']}) {t['title']}" for t in todos]
        project_ctx_parts.append("Existing tasks:\n" + "\n".join(task_lines))

    project_context = "\n\n".join(project_ctx_parts)

    # ── Build tools documentation for system prompt ──────────────────
    tools_doc_parts: list[str] = []
    has_builtins = any(t.get("_builtin") for t in all_tools) if all_tools else False
    if has_builtins:
        tools_doc_parts.append(
            "Tools: read_file, list_directory, search_files, semantic_search, "
            "run_command (git, grep, build/test commands — no file writes).\n"
            "Deps at ../deps/{name}/. Use run_command for git log, git blame, grep -rn, etc.\n"
            "You have project context above — use tools only for specific code details not already known."
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

    system_prompt += "\n\nMessages from different users are prefixed with [Name]. Address them by name when relevant."

    # ── Load & compact chat history ──────────────────────────────────
    history = await db.fetch(
        "SELECT m.role, m.content, u.display_name AS sender_name "
        "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = $1 ORDER BY m.created_at DESC LIMIT 40",
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
        content = row["content"]
        if row["role"] == "user" and row.get("sender_name"):
            content = f"[{row['sender_name']}]: {content}"
        messages.append(LLMMessage(role=row["role"], content=content))
    current_content = f"[{user_display_name}]: {user_message}" if user_display_name else user_message
    messages.append(LLMMessage(role="user", content=current_content))

    # ── Tool executor ────────────────────────────────────────────────
    mcp_exec = McpToolExecutor(db)

    async def _execute_tool(name: str, arguments: dict) -> str:
        return await mcp_exec.execute_tool(name, arguments, all_tools)

    on_activity = await _build_on_activity(redis, session_id)
    on_token = await _build_on_token(redis, session_id)

    # Allow users to inject guidance while the planner explores
    _inject_key = f"chat:session:{session_id}:inject"

    async def _check_inject() -> str | None:
        if redis:
            return await redis.lpop(_inject_key)
        return None

    # ── Collect tool execution events for UI visibility ────────────
    tool_events: list[dict] = []

    async def _on_tool_event(event: dict) -> None:
        # Cap tool_events to prevent unbounded memory growth during long loops.
        # Keep all llm_thinking events (small, needed for token stats) but
        # trim tool_result/tool_start entries to the most recent 200.
        if len(tool_events) > 500:
            # Keep llm_thinking events + last 200 others
            thinking = [e for e in tool_events if e.get("type") == "llm_thinking"]
            others = [e for e in tool_events if e.get("type") != "llm_thinking"]
            tool_events[:] = thinking + others[-200:]
        tool_events.append(event)

    # ── Run tool loop ────────────────────────────────────────────────
    # Plan mode needs more rounds — the agent explores extensively before
    # proposing a plan (reading files, searching patterns, listing dirs).
    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=30,
        on_activity=on_activity,
        on_tool_event=_on_tool_event,
        on_inject_check=_check_inject,
        on_token=on_token,
        **plan_send_kwargs,
    )

    if on_token and hasattr(on_token, "flush"):
        await on_token.flush()

    # Guard against empty responses — retry once without streaming
    if not content or not content.strip():
        logger.warning("[plan_mode] empty response, retrying without streaming session=%s", session_id)
        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=_execute_tool,
            max_rounds=30,
            on_activity=on_activity,
            on_tool_event=_on_tool_event,
            **plan_send_kwargs,
        )
    if not content or not content.strip():
        content = "I wasn't able to generate a response. Could you rephrase your request?"

    # Drain unconsumed inject messages
    if redis:
        while await redis.lpop(_inject_key):
            pass

    # ── Build execution metadata for UI ──────────────────────────────
    execution_meta = _build_execution_metadata(tool_events, response)

    # ── Plan extraction & acceptance ─────────────────────────────────
    metadata = None
    if "```json" in content and '"action"' in content and '"create_plan"' in content:
        try:
            # Use rindex to find the *last* JSON block (avoids false matches
            # from tool results that might contain markdown JSON fences).
            json_start = content.rindex("```json") + 7
            json_end = content.index("```", json_start)
            raw_plan_json = content[json_start:json_end].strip()
            logger.info(
                "[plan_mode] session=%s raw plan JSON from LLM:\n%s",
                session_id, raw_plan_json[:5000],
            )
            plan_data = json.loads(raw_plan_json)

            if plan_data.get("action") == "create_plan":
                # Log per-task target_repo for debugging
                for ti, task in enumerate(plan_data.get("tasks", [])):
                    for si, st in enumerate(task.get("subtasks", [])):
                        logger.info(
                            "[plan_mode]   task[%d].subtask[%d]: role=%s target_repo=%r title=%r",
                            ti, si, st.get("agent_role"), st.get("target_repo", "(MISSING)"), st.get("title"),
                        )

                await db.execute(
                    "UPDATE project_chat_sessions SET plan_json = $2, updated_at = NOW() WHERE id = $1",
                    session_id,
                    plan_data,
                )
                metadata = {
                    "action": "plan_proposed",
                    "plan_title": plan_data.get("plan_title", ""),
                    "task_count": len(plan_data.get("tasks", [])),
                    "plan_data": plan_data,
                }
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            logger.warning(
                "[plan_mode] session=%s plan JSON parse failed: %s\nRaw content tail:\n%s",
                session_id, exc, content[-2000:],
            )

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

    # Merge execution metadata into the message metadata
    if metadata is None:
        metadata = {}
    metadata["execution"] = execution_meta

    if placeholder_id:
        msg_row = await db.fetchrow(
            """
            UPDATE project_chat_messages
            SET content = $2, metadata_json = $3, updated_at = NOW()
            WHERE id = $1 RETURNING *
            """,
            placeholder_id, content, metadata,
        )
    else:
        msg_row = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
            VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
            """,
            project_id, user_id, content, metadata, session_id,
        )
    tool_events.clear()
    result = dict(msg_row)
    del msg_row, content, metadata, response
    gc.collect()
    return result


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

    # Load context_docs once for target_repo resolution
    project_row = await db.fetchrow(
        "SELECT context_docs FROM projects WHERE id = $1", project_id,
    )
    context_docs = []
    if project_row and project_row.get("context_docs"):
        raw = project_row["context_docs"]
        context_docs = json.loads(raw) if isinstance(raw, str) else raw

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
                        "target_repo": st.get("target_repo", "main"),
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
                VALUES ($1, $2, $3, $4, $5, $6, 'in_progress', 'executing', $7, $8)
                RETURNING *
                """,
                project_id,
                user_id,
                task["title"],
                task.get("description", ""),
                task.get("priority", "medium"),
                task.get("task_type", "general"),
                plan_json,
                intake_data,
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
                target_repo = resolve_target_repo(st.get("target_repo"), context_docs)
                logger.info(
                    "[create_tasks] subtask[%d]: target_repo raw=%r resolved=%s",
                    i, st.get("target_repo"), target_repo.get("name") if target_repo else None,
                )
                row = await db.fetchrow(
                    """
                    INSERT INTO sub_tasks (
                        todo_id, title, description, agent_role,
                        execution_order, input_context, review_loop, target_repo
                    )
                    VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, $6, $7)
                    RETURNING id
                    """,
                    todo_id,
                    st["title"],
                    st.get("description", ""),
                    st["agent_role"],
                    st.get("execution_order", 0),
                    review_loop,
                    target_repo,
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

## Git History as a Debugging Tool
When investigating a bug, recent code changes are a high-signal source. \
Use `run_command` with git to sample recent diffs:
- `git log --oneline -N` to scan recent commit messages for relevant changes
- `git diff HEAD~N..HEAD -- <path>` to see diffs in a specific area
- `git log --oneline --all -- <file>` to find when a file was last changed
- `git blame <file>` to see who last touched each line
Start with the commit log, then drill into diffs for commits that look relevant \
to the bug area. The user may tell you how far back to look; otherwise use your \
judgment (start small, widen if needed — max ~50 commits).

{recent_commits}

{tools_doc}

{debug_context}

Be thorough but conversational. Share findings incrementally."""


async def generate_debug_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    user_display_name: str = "",
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
    placeholder_id: str | None = None,
) -> dict:
    """Generate an AI response in debug mode."""
    from agents.agents.registry import get_builtin_tool_schemas
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = get_registry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    tools_reg = ToolsRegistry(db)
    mcp_tools = await tools_reg.resolve_tools(
        project_id=project_id, user_id=user_id,
    )

    workspace_path = project.get("workspace_path") or ""
    builtin_tools = get_builtin_tool_schemas(workspace_path, "debugger") if workspace_path else []

    # Inject semantic_search index dirs (same pattern as plan mode)
    if workspace_path and builtin_tools:
        import os
        _debug_idx = os.path.join(workspace_path, ".agent_index")
        _debug_deps_idx_root = os.path.join(workspace_path, ".agent_index_deps")
        _debug_dep_idx_dirs: dict[str, str] = {}
        if os.path.isdir(_debug_deps_idx_root):
            try:
                for entry in os.listdir(_debug_deps_idx_root):
                    idx_dir = os.path.join(_debug_deps_idx_root, entry)
                    if os.path.isdir(idx_dir):
                        _debug_dep_idx_dirs[entry] = idx_dir
            except OSError:
                pass
        for bt in builtin_tools:
            if bt["name"] == "semantic_search":
                bt["_index_dir"] = _debug_idx
                if _debug_dep_idx_dirs:
                    bt["_dep_index_dirs"] = _debug_dep_idx_dirs

    project_ctx_parts = []
    if project.get("description"):
        project_ctx_parts.append(f"Description: {project['description']}")
    if project.get("repo_url"):
        project_ctx_parts.append(f"Repository: {project['repo_url']}")

    settings = safe_json(project.get("settings_json"))
    debug_context_str = ""
    if settings:
        # Full shared context: summary, tech stack, cross-repo links, dep map, work rules
        shared_ctx = _build_project_context(project, settings)
        if shared_ctx:
            project_ctx_parts.append(shared_ctx)

        # Debug-specific context (log sources with commands, MCP hints with notes)
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

    # Inject repo map for structural awareness (same as plan mode)
    if workspace_path:
        import os
        repo_dir = os.path.join(workspace_path, "repo")
        if os.path.isdir(repo_dir):
            try:
                repo_map_path = os.path.join(workspace_path, ".agent_index", "repo_map.txt")
                if os.path.isfile(repo_map_path):
                    with open(repo_map_path, "r") as f:
                        repo_map = f.read()
                    if repo_map:
                        if len(repo_map) > 3000:
                            repo_map = repo_map[:3000] + "\n..."
                        project_ctx_parts.append(f"Repo map:\n{repo_map}")
            except Exception:
                pass

    project_context = "\n".join(project_ctx_parts)

    # Fetch recent git log to give the debugger immediate commit context
    recent_commits_str = ""
    if workspace_path:
        import os
        import subprocess

        repo_dir = os.path.join(workspace_path, "repo")
        if os.path.isdir(repo_dir):
            try:
                result = subprocess.run(
                    ["git", "log", "--oneline", "--no-decorate", "-20"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    recent_commits_str = (
                        "## Recent Commits (last 20)\n"
                        "Use these as a starting point — drill into diffs for commits "
                        "that look relevant to the issue.\n```\n"
                        + result.stdout.strip()
                        + "\n```"
                    )
            except Exception:
                pass

    tools_doc_parts = []
    if builtin_tools:
        tools_doc_parts.append(
            "## Tools Available\n"
            "You have workspace tools to explore the codebase and run commands:\n"
            "- **read_file** — Read a file's contents (path relative to repo root)\n"
            "- **list_directory** — List files and directories\n"
            "- **search_files** — Search for a text pattern across files (grep)\n"
            "- **semantic_search** — Natural language search across the codebase\n"
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
        recent_commits=recent_commits_str,
        tools_doc=tools_doc,
        debug_context=debug_context_str,
    )

    history = await db.fetch(
        "SELECT m.role, m.content, u.display_name AS sender_name "
        "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = $1 ORDER BY m.created_at DESC LIMIT 30",
        session_id,
    )
    history = list(reversed(history))

    system_prompt += "\n\nMessages from different users are prefixed with [Name]. Address them by name when relevant."

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        content = row["content"]
        if row["role"] == "user" and row.get("sender_name"):
            content = f"[{row['sender_name']}]: {content}"
        messages.append(LLMMessage(role=row["role"], content=content))
    current_content = f"[{user_display_name}]: {user_message}" if user_display_name else user_message
    messages.append(LLMMessage(role="user", content=current_content))

    from agents.providers.base import run_tool_loop

    all_tools = builtin_tools + (mcp_tools or [])
    tools_arg = all_tools if all_tools else None

    mcp_exec = McpToolExecutor(db)

    async def _execute_tool(name: str, arguments: dict) -> str:
        return await mcp_exec.execute_tool(name, arguments, all_tools)

    on_activity = await _build_on_activity(redis, session_id)
    on_token = await _build_on_token(redis, session_id)

    # Allow users to inject guidance while the debugger investigates
    _inject_key = f"chat:session:{session_id}:inject"

    async def _check_inject() -> str | None:
        if redis:
            return await redis.lpop(_inject_key)
        return None

    # Collect tool execution events for UI visibility
    tool_events: list[dict] = []

    async def _on_tool_event(event: dict) -> None:
        # Cap tool_events to prevent unbounded memory growth during long loops.
        # Keep all llm_thinking events (small, needed for token stats) but
        # trim tool_result/tool_start entries to the most recent 200.
        if len(tool_events) > 500:
            # Keep llm_thinking events + last 200 others
            thinking = [e for e in tool_events if e.get("type") == "llm_thinking"]
            others = [e for e in tool_events if e.get("type") != "llm_thinking"]
            tool_events[:] = thinking + others[-200:]
        tool_events.append(event)

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override

    content, response = await run_tool_loop(
        provider, messages,
        tools=tools_arg,
        tool_executor=_execute_tool,
        max_rounds=20,
        nudge_tools=False,
        on_activity=on_activity,
        on_tool_event=_on_tool_event,
        on_inject_check=_check_inject,
        on_token=on_token,
        **send_kwargs,
    )

    if on_token and hasattr(on_token, "flush"):
        await on_token.flush()

    # Guard against empty responses
    if not content or not content.strip():
        logger.warning("[debug] empty response, retrying without streaming session=%s", session_id)
        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=_execute_tool,
            max_rounds=20,
            nudge_tools=False,
            on_activity=on_activity,
            on_tool_event=_on_tool_event,
            **send_kwargs,
        )
    if not content or not content.strip():
        content = "I wasn't able to generate a response. Could you rephrase your request?"

    if redis:
        while await redis.lpop(_inject_key):
            pass

    # Summarize findings when the agent explored extensively
    rounds = sum(1 for e in tool_events if e.get("type") == "llm_thinking")
    raw_output = None
    if rounds >= 3 and content and len(content) > 4000:
        raw_output = content
        content = await _summarize_findings(
            provider, user_message, content,
            on_activity=on_activity,
            **send_kwargs,
        )

    execution_meta = _build_execution_metadata(tool_events, response)
    metadata = {"execution": execution_meta}
    if raw_output:
        metadata["raw_output"] = raw_output[:20000]

    if placeholder_id:
        msg_row = await db.fetchrow(
            """
            UPDATE project_chat_messages
            SET content = $2, metadata_json = $3, updated_at = NOW()
            WHERE id = $1 RETURNING *
            """,
            placeholder_id, content, metadata,
        )
    else:
        msg_row = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
            VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
            """,
            project_id, user_id, content, metadata, session_id,
        )
    tool_events.clear()
    result = dict(msg_row)
    del msg_row, content, metadata, response
    gc.collect()
    return result


# ── Create Task Mode ────────────────────────────────────────────────


CREATE_TASK_SYSTEM = """\
You are a task creation assistant for "{project_name}".
{project_context}

The user wants to create a task. Your job:
1. GATHER REQUIREMENTS FIRST — Before creating any task, make sure you understand:
   - What exactly needs to be done (the scope and expected outcome)
   - Which parts of the codebase are affected
   - Any constraints, dependencies, or preferences
2. If the user's description is vague or missing key details, ask 1-2 clarifying questions.
   Do NOT create a task until you have enough information to define clear sub_tasks.
3. Once you have a clear picture, use action__create_task to create it.
4. Include sub_tasks with appropriate agent roles.
5. Every sub_task MUST have target_repo set — you must know which repo each sub_task targets.

IMPORTANT: Do NOT rush to create. A well-specified task with clear sub_tasks is worth \
more than a quick but vague task. Ask questions first if the scope is unclear.

{tools_doc}

{repos_doc}

Valid agent roles for sub_tasks: coder, tester, reviewer, pr_creator, report_writer
Valid task types: code, research, document, general
Valid priorities: critical, high, medium, low"""


async def generate_create_task_response(
    *,
    project_id: str,
    session_id: str,
    user_id: str,
    user_message: str,
    user_display_name: str = "",
    project: dict,
    db,
    event_bus=None,
    redis=None,
    model_override: str | None = None,
    placeholder_id: str | None = None,
) -> dict:
    """Generate a response in create-task mode."""
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.schemas.agent import LLMMessage

    registry = get_registry(db)
    provider = await registry.resolve_for_project(project_id, user_id)

    action_tools = get_actions_as_tools("project")
    action_tools = [t for t in (action_tools or []) if "create_task" in t.get("name", "")]

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
  IMPORTANT: Always create ONE task with ALL sub_tasks inside it. Every sub_task MUST have target_repo."""

    # Build repos documentation
    context_docs = project.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)
    dep_names = [
        d["name"] for d in context_docs
        if isinstance(d, dict) and d.get("name") and d.get("repo_url")
    ]

    repos_parts = [f"Main repository: {project.get('repo_url') or '(not configured)'}"]
    if dep_names:
        dep_str = ", ".join(f"'{n}'" for n in dep_names)
        repos_parts.append(f"Configured dependency repos: {dep_str}")
        repos_parts.append(
            "Set target_repo='main' for main repo work, or the EXACT dependency name for dep work."
        )
    else:
        repos_parts.append("No dependency repos configured — use target_repo='main' for all sub_tasks.")
    repos_doc = "\n".join(repos_parts)

    system_prompt = CREATE_TASK_SYSTEM.format(
        project_name=project["name"],
        project_context=project_context,
        tools_doc=tools_doc,
        repos_doc=repos_doc,
    )

    history = await db.fetch(
        "SELECT m.role, m.content, u.display_name AS sender_name "
        "FROM project_chat_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = $1 ORDER BY m.created_at DESC LIMIT 20",
        session_id,
    )
    history = list(reversed(history))

    system_prompt += "\n\nMessages from different users are prefixed with [Name]. Address them by name when relevant."

    messages = [LLMMessage(role="system", content=system_prompt)]
    for row in history[:-1]:
        content = row["content"]
        if row["role"] == "user" and row.get("sender_name"):
            content = f"[{row['sender_name']}]: {content}"
        messages.append(LLMMessage(role=row["role"], content=content))
    current_content = f"[{user_display_name}]: {user_message}" if user_display_name else user_message
    messages.append(LLMMessage(role="user", content=current_content))

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
            logger.info(
                "[create_task_mode] session=%s tool=%s raw_arguments:\n%s",
                session_id, name,
                json.dumps(arguments, indent=2, default=str)[:5000],
            )
            result_text = await execute_action(name, arguments, action_context)
            try:
                result_data = json.loads(result_text)
                if result_data.get("action") == "task_created":
                    metadata = {
                        "action": result_data["action"],
                        "task_id": result_data.get("task_id"),
                        "task_title": result_data.get("title"),
                    }
                    if result_data.get("awaiting_approval") and result_data.get("plan_data"):
                        metadata["action"] = "task_plan_ready"
                        metadata["plan_data"] = result_data["plan_data"]
                elif result_data.get("action") == "task_plan_ready":
                    # Deferred creation: plan stored in session, no task_id yet
                    metadata = {
                        "action": "task_plan_ready",
                        "task_title": result_data.get("title"),
                        "plan_data": result_data.get("plan_data"),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
            return result_text
        return json.dumps({"error": f"Unknown tool: {name}"})

    on_activity = await _build_on_activity(redis, session_id)
    on_token = await _build_on_token(redis, session_id)

    tool_events: list[dict] = []

    async def _on_tool_event(event: dict) -> None:
        # Cap tool_events to prevent unbounded memory growth during long loops.
        # Keep all llm_thinking events (small, needed for token stats) but
        # trim tool_result/tool_start entries to the most recent 200.
        if len(tool_events) > 500:
            # Keep llm_thinking events + last 200 others
            thinking = [e for e in tool_events if e.get("type") == "llm_thinking"]
            others = [e for e in tool_events if e.get("type") != "llm_thinking"]
            tool_events[:] = thinking + others[-200:]
        tool_events.append(event)

    send_kwargs: dict = {}
    if model_override:
        send_kwargs["model"] = model_override

    content, response = await run_tool_loop(
        provider, messages,
        tools=action_tools if action_tools else None,
        tool_executor=_execute_tool,
        max_rounds=8,
        nudge_tools=False,
        on_activity=on_activity,
        on_tool_event=_on_tool_event,
        on_token=on_token,
        **send_kwargs,
    )

    if on_token and hasattr(on_token, "flush"):
        await on_token.flush()

    # Guard against empty responses
    if not content or not content.strip():
        logger.warning("[create_task] empty response, retrying without streaming session=%s", session_id)
        content, response = await run_tool_loop(
            provider, messages,
            tools=action_tools if action_tools else None,
            tool_executor=_execute_tool,
            max_rounds=8,
            nudge_tools=False,
            on_activity=on_activity,
            on_tool_event=_on_tool_event,
            **send_kwargs,
        )
    if not content or not content.strip():
        content = "I wasn't able to generate a response. Could you rephrase your request?"

    execution_meta = _build_execution_metadata(tool_events, response)
    if metadata is None:
        metadata = {}
    metadata["execution"] = execution_meta

    if placeholder_id:
        msg_row = await db.fetchrow(
            """
            UPDATE project_chat_messages
            SET content = $2, metadata_json = $3, updated_at = NOW()
            WHERE id = $1 RETURNING *
            """,
            placeholder_id, content, metadata,
        )
    else:
        msg_row = await db.fetchrow(
            """
            INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id)
            VALUES ($1, $2, 'assistant', $3, $4, $5) RETURNING *
            """,
            project_id, user_id, content, metadata, session_id,
        )
    tool_events.clear()
    result = dict(msg_row)
    del msg_row, content, metadata, response
    gc.collect()
    return result
