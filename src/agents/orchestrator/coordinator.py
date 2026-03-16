"""Agent Coordinator: the per-task brain.

Each TODO item gets one AgentCoordinator instance. It manages:
1. INTAKE: AI interviewer that asks thorough questions upfront
2. PLANNING: Decomposes task into sub-tasks with agent assignments
3. EXECUTION: Runs sub-tasks in parallel (respecting dependencies)
4. TESTING: Installs deps, builds, runs tests — loops back for fixes if needed
5. REVIEW: Auto-reviews deliverables, only escalates if needed
6. CHAT: Handles user messages between steps for steering
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis

from agents.config.settings import settings
from agents.infra.notifier import Notifier
from agents.orchestrator.workspace import WorkspaceManager
from agents.orchestrator.state_machine import (
    check_all_subtasks_done,
    transition_subtask as _transition_subtask,
    transition_todo as _transition_todo,
)
from agents.providers.base import AIProvider
from agents.providers.mcp_executor import McpToolExecutor
from agents.providers.registry import ProviderRegistry
from agents.providers.tools_registry import ToolsRegistry
from agents.schemas.agent import LLMMessage, LLMResponse
from agents.utils.json_helpers import parse_llm_json, safe_json

logger = logging.getLogger(__name__)

from agents.agents.registry import (
    build_tools_prompt_block,
    get_agent_definition,
    get_builtin_tool_schemas,
    get_default_system_prompt,
)

MAX_REVIEW_ROUNDS = 10

STUCK_DETECTOR_PROMPT = """\
Analyze the last 5 iteration logs for loop patterns (same fix repeated, flip-flopping, \
undoing changes, same error recurring). Respond with JSON only:
{"stuck": true/false, "pattern": "loop description or null", "advice": "actionable fix or null"}
"""


INTAKE_SYSTEM_PROMPT = """\
You are a task intake agent. Decide if the task is clear enough to start.

Default to ready=true. Only set ready=false for genuinely ambiguous tasks with \
contradictory requirements or multiple fundamentally different interpretations.

Output JSON only:
{"ready": true, "requirements": "concise summary", "approach": "one-line approach", "auto_answerable": []}
or
{"ready": false, "questions": ["1-2 critical questions only"]}
"""

PLANNER_SYSTEM_PROMPT = """\
You are a senior project planner. Your job is to explore the codebase, understand the \
existing architecture, and decompose the task into well-structured sub-tasks for specialist agents.

## Workflow
1. FIRST, use the workspace tools (read_file, list_directory, search_files, run_command) \
to explore the codebase. Understand the project structure, relevant files, existing patterns, \
and conventions BEFORE creating a plan.
2. Read relevant source files to understand what already exists and what needs to change.
3. Only AFTER exploring, output your execution plan as a JSON object.

## Planning Rules

CRITICAL — ONE TASK, MANY SUB-TASKS:
- Decompose the work into focused sub-tasks. Each sub-task should be one unit of work \
(one file, one feature, one concern).
- Use depends_on (0-based sub-task indexes) to express ordering between sub-tasks.
- Maximize parallelism: sub-tasks with no dependencies run concurrently.

AGENT ROLES:
- **coder** — Implements code, fixes bugs, adds features. Use for all code changes.
- **debugger** — Investigates and fixes bugs using logs, metrics, database queries, and VM access. \
Use for bug reports, error investigations, production incidents, and performance issues.
- **tester** — Writes and runs tests. Use after coder sub-tasks to validate changes.
- **reviewer** — Reviews code quality, checks for bugs/security. Use for important changes.
- **report_writer** — Generates documentation and reports.

NOTE: Do NOT create pr_creator or merge_agent sub-tasks in your plan. \
PR creation and merging are handled automatically by the system after coder work completes.

REVIEW LOOP (review_loop field):
- Set review_loop=true for critical or complex code changes that need the full \
coder→reviewer→PR→merge cycle. The system will automatically chain a reviewer, PR creator, \
and merge agent after the coder completes.
- Use review_loop=true for: core business logic, security-sensitive code, API changes, \
database migrations, infrastructure changes.
- Use review_loop=false for: simple fixes, config changes, documentation, test-only changes, \
straightforward additions.

SUB-TASK DESCRIPTIONS:
- Be specific and detailed. Include which files to modify, what patterns to follow, \
and what the expected outcome is.
- Reference actual file paths and code patterns you discovered during exploration.
- Include relevant context from your codebase exploration so the agent doesn't need to \
re-discover everything.

## Cross-Repo Exploration

Dependency repos are available at ../deps/{name}/ (read-only via workspace tools).
- Use list_directory(path="../deps/") to see which dependency repos are available.
- Use read_file(path="../deps/{name}/src/...") to read dependency source code.
- Use search_files(pattern="...", path="../deps/{name}/") to search within a dependency.
- When a task involves cross-repo concerns (shared types, API consumed from a dep, \
integration patterns), ALWAYS explore both the main repo AND the relevant deps first.
- For sub-tasks that need to modify a dependency repo, set target_repo with the dep's \
repo_url, name, default_branch, and git_provider_id.

## Query Enrichment

Before creating your plan, proactively enrich the user's request:
- Identify ambiguities or missing context in the task description.
- Explore the codebase to find the actual file paths, current implementation, and patterns.
- Discover the current behavior so you can describe what needs to change.
- Make each sub-task description self-contained with all discovered context \
(file paths, function names, current patterns) so agents can start working immediately.

## Output Format

After exploring the codebase, output ONLY a JSON object (no markdown fences, no extra text):
{"summary":"...", "sub_tasks":[{"title":"...", "description":"...", "agent_role":"...", \
"execution_order":0, "depends_on":[], "review_loop":false, "target_repo":null}], "estimated_tokens":5000}

Sub-task fields:
- title: short descriptive title
- description: detailed instructions for the agent
- agent_role: one of the roles above
- execution_order: 0 for parallel, sequential number for ordered execution
- depends_on: list of 0-based indexes of sub-tasks this depends on
- review_loop: true for critical code changes needing coder→reviewer→merge cycle
- target_repo: null unless working on a different repo \
(format: {"repo_url":"...", "name":"...", "default_branch":"main", "git_provider_id":null})
"""

REVIEW_SYSTEM_PROMPT = """\
You are a work reviewer. Approve unless there are objective problems (broken logic, \
missing critical requirements, security issues). Style nits are not blockers.

Output JSON: {"approved": true/false, "issues": [...], "summary": "..."}
"""


def _classify_error(exc: Exception) -> str:
    """Classify an exception into a category for error_type field."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(k in name for k in ("timeout", "timedout")):
        return "timeout"
    if any(k in name for k in ("ratelimit", "rate_limit", "429")):
        return "rate_limit"
    if "401" in msg or "unauthorized" in msg or "authentication" in msg:
        return "auth_error"
    if "context" in msg and ("long" in msg or "length" in msg or "tokens" in msg):
        return "context_length"
    if any(k in name for k in ("connection", "network", "dns")):
        return "network"
    if "json" in name or "parse" in name or "decode" in name:
        return "parse_error"
    return "transient"


class AgentCoordinator:
    def __init__(
        self,
        todo_id: str,
        db: asyncpg.Pool,
        redis: aioredis.Redis,
        provider_registry: ProviderRegistry,
        notifier: Notifier,
    ):
        self.todo_id = todo_id
        self.db = db
        self.redis = redis
        self.provider_registry = provider_registry
        self.notifier = notifier
        self.workspace_mgr = WorkspaceManager(db, settings.workspace_root)
        self.tools_registry = ToolsRegistry(db)
        self.mcp_executor = McpToolExecutor(db)
        # Activity throttle state (per-subtask timestamps)
        self._last_activity_publish: dict[str, float] = {}
        self._last_activity_persist: dict[str, float] = {}

    async def _transition_todo(self, target_state: str, **kwargs) -> dict | None:
        """Wrapper that auto-passes db, todo_id, and redis for WS publishing."""
        return await _transition_todo(
            self.db, self.todo_id, target_state, redis=self.redis, **kwargs,
        )

    async def _transition_subtask(self, subtask_id: str, target_status: str, **kwargs) -> dict | None:
        """Wrapper that auto-passes db and redis for WS publishing."""
        return await _transition_subtask(
            self.db, subtask_id, target_status, redis=self.redis, **kwargs,
        )

    async def _resolve_agent_config(self, role: str, owner_id: str) -> dict | None:
        """Look up a custom agent_config for the given role.

        Returns the row dict if a matching active config exists, else None
        (caller should fall back to default prompts).
        """
        row = await self.db.fetchrow(
            "SELECT * FROM agent_configs WHERE role = $1 AND owner_id = $2 AND is_active = TRUE "
            "ORDER BY updated_at DESC LIMIT 1",
            role,
            owner_id,
        )
        return dict(row) if row else None

    async def run(self) -> None:
        """Main coordinator loop for one task."""
        todo = await self._load_todo()
        logger.info("[%s] coordinator.run(): state=%s sub_state=%s title=%s",
                    self.todo_id, todo["state"], todo.get("sub_state"), todo.get("title"))

        # Resolve linked chat session for message routing
        self._chat_session_id = str(todo["chat_session_id"]) if todo.get("chat_session_id") else None
        self._chat_project_id = str(todo["project_id"]) if self._chat_session_id else None
        self._chat_user_id = str(todo["creator_id"]) if self._chat_session_id else None

        provider = await self.provider_registry.resolve_for_todo(self.todo_id)
        logger.info("[%s] Provider resolved: %s/%s",
                    self.todo_id, provider.provider_type, provider.default_model)

        match todo["state"]:
            case "intake":
                logger.info("[%s] → entering _phase_intake", self.todo_id)
                await self._phase_intake(todo, provider)
            case "planning":
                logger.info("[%s] → entering _phase_planning", self.todo_id)
                await self._phase_planning(todo, provider)
            case "plan_ready":
                # Auto-approve if plan exists — minimal human gating
                plan = todo.get("plan_json")
                logger.info("[%s] → plan_ready: plan exists=%s", self.todo_id, plan is not None)
                if plan:
                    if isinstance(plan, str):
                        plan = json.loads(plan)
                    logger.info("[%s] → entering _auto_approve_plan (sub_tasks=%d)",
                                self.todo_id, len(plan.get("sub_tasks", [])))
                    await self._auto_approve_plan(todo, plan)
                else:
                    logger.warning("[%s] plan_ready but no plan_json! Transitioning back to planning",
                                   self.todo_id)
                    await self._transition_todo("planning", sub_state="re_planning_no_plan")
            case "in_progress":
                logger.info("[%s] → entering _phase_execution", self.todo_id)
                await self._phase_execution(todo, provider)
            case "testing":
                logger.info("[%s] → entering _phase_testing", self.todo_id)
                await self._phase_testing(todo, provider)
            case _:
                logger.warning("[%s] coordinator.run() unhandled state: %s", self.todo_id, todo["state"])

    # ---- INTAKE PHASE ----

    async def _phase_intake(self, todo: dict, provider: AIProvider) -> None:
        """Gather all requirements upfront via AI interview."""
        context = await self._build_context(todo)

        messages = [
            LLMMessage(role="system", content=INTAKE_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Task: {todo['title']}\n"
                    f"Description: {todo['description'] or 'No description provided'}\n"
                    f"Type: {todo['task_type']}\n"
                    f"Project context: {json.dumps(context, default=str)}"
                ),
            ),
        ]

        # Check if there are existing chat messages (user may have already answered questions)
        chat_history = await self._load_chat_history()
        if chat_history:
            for msg in chat_history:
                messages.append(LLMMessage(role=msg["role"], content=msg["content"]))

        response = await provider.send_message(messages, temperature=0.2)
        await self._track_tokens(response)

        result = parse_llm_json(response.content)
        if result is None:
            # Retry once with a correction prompt before defaulting
            logger.warning("Intake: failed to parse JSON, retrying with correction prompt")
            messages.append(LLMMessage(role="assistant", content=response.content))
            messages.append(LLMMessage(
                role="user",
                content=(
                    "Your response was not valid JSON. Please respond with ONLY a JSON object "
                    'with keys: "ready" (boolean), "requirements" (string or object), '
                    '"approach" (string). If you need to ask questions, include '
                    '"questions" (list of strings) and set "ready" to false.'
                ),
            ))
            retry_response = await provider.send_message(messages, temperature=0.1)
            await self._track_tokens(retry_response)
            result = parse_llm_json(retry_response.content)

        if result is None:
            # If retry also failed, default to ready with the task title as requirements
            logger.warning("Intake: JSON parse failed after retry, defaulting to ready=true")
            result = {
                "ready": True,
                "requirements": todo["title"],
                "approach": "Determined from task description and project context",
            }

        if result.get("ready", False):
            # Enough info gathered — store intake data and move to planning
            intake_data = {
                "requirements": result.get("requirements", {}),
                "approach": result.get("approach", ""),
                "auto_answers": result.get("auto_answerable", []),
            }
            await self.db.execute(
                "UPDATE todo_items SET intake_data = $2 WHERE id = $1",
                self.todo_id,
                intake_data,
            )
            await self._post_system_message(
                f"**Intake complete.** Moving to planning.\n\n"
                f"**Requirements:** {json.dumps(result.get('requirements', {}), indent=2)}\n\n"
                f"**Approach:** {result.get('approach', 'Auto-determined')}"
            )
            await self._transition_todo( "planning", sub_state="decomposing")
            # Immediately continue to planning
            todo = await self._load_todo()
            await self._phase_planning(todo, provider)
        else:
            # Need to ask human questions — mark as awaiting_response so the
            # orchestrator loop does NOT re-dispatch until the user replies.
            questions = result.get("questions", [])
            if questions:
                q_text = "\n".join(f"- {q}" for q in questions)
                await self._post_system_message(
                    f"**Quick question before proceeding:**\n\n{q_text}\n\n"
                    "Reply in the chat, or the task will auto-proceed with reasonable defaults."
                )
                await self.db.execute(
                    "UPDATE todo_items SET sub_state = 'awaiting_response', updated_at = NOW() WHERE id = $1",
                    self.todo_id,
                )

    # ---- PLANNING PHASE ----

    async def _phase_planning(self, todo: dict, provider: AIProvider) -> None:
        """Explore codebase with tools, then decompose task into sub-tasks."""
        if todo["state"] != "planning":
            await self._transition_todo( "planning", sub_state="decomposing")
        else:
            await self.db.execute(
                "UPDATE todo_items SET sub_state = 'decomposing', updated_at = NOW() WHERE id = $1",
                self.todo_id,
            )

        context = await self._build_context(todo)
        intake_data = safe_json(todo.get("intake_data"))

        # Inject available custom agents into planner context
        custom_agents = await self.db.fetch(
            "SELECT role, name, description FROM agent_configs "
            "WHERE owner_id = $1 AND is_active = TRUE",
            todo["creator_id"],
        )
        available_roles = "coder, debugger, tester, reviewer, report_writer"
        if custom_agents:
            custom_lines = [f"  - {a['role']}: {a['name']} — {a['description'] or 'no description'}" for a in custom_agents]
            available_roles += "\n\nCustom agents available:\n" + "\n".join(custom_lines)

        planner_prompt = PLANNER_SYSTEM_PROMPT + f"\n\nAvailable agent roles: {available_roles}\n"

        # Resolve workspace for codebase exploration
        workspace_path = None
        try:
            project = await self.db.fetchrow(
                "SELECT repo_url, workspace_path FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if project and project.get("repo_url"):
                workspace_path = await self.workspace_mgr.setup_task_workspace(self.todo_id)
                logger.info("[%s] Planner workspace ready at %s", self.todo_id, workspace_path)
        except Exception:
            logger.warning("[%s] Could not set up planner workspace", self.todo_id, exc_info=True)

        # Add workspace context and tool instructions to system prompt
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=3)
            planner_prompt += (
                f"\n\nProject file structure:\n{file_tree}\n\n"
                "You MUST explore the codebase using the tools before outputting your plan. "
                "Read relevant files, search for patterns, and understand the existing code."
            )

            # Inject dependency info so planner knows about available deps
            dep_dirs = context.get("dependency_dirs", {})
            deps_list = context.get("dependencies", [])
            if dep_dirs:
                planner_prompt += "\n\nDependency repositories available for reference (read-only):"
                planner_prompt += "\nAccess via ../deps/{name}/path relative to repo root."
                for dep in deps_list:
                    name = dep.get("name", "")
                    dir_name = dep_dirs.get(name, "")
                    if dir_name:
                        line = f"\n  - ../deps/{dir_name}/"
                        if dep.get("repo_url"):
                            line += f" ({dep['repo_url']})"
                        if dep.get("git_provider_id"):
                            line += f" [git_provider_id: {dep['git_provider_id']}]"
                        planner_prompt += line

            # Inject cross-repo integration links from project understanding
            understanding = context.get("project_understanding", {})
            cross_links = understanding.get("cross_repo_links", []) if isinstance(understanding, dict) else []
            if cross_links:
                planner_prompt += "\n\nKnown cross-repo integration points:"
                for link in cross_links:
                    dep = link.get("dep_name", "?")
                    pattern = link.get("integration_pattern", "")
                    main_files = ", ".join(link.get("main_repo_files", [])[:5])
                    interfaces = ", ".join(link.get("shared_interfaces", [])[:5])
                    planner_prompt += f"\n  - {dep}: {pattern}"
                    if main_files:
                        planner_prompt += f" (main repo: {main_files})"
                    if interfaces:
                        planner_prompt += f" (interfaces: {interfaces})"

            planner_prompt += build_tools_prompt_block("planner")

        # Build user message — add explicit retry/diff context if present
        user_parts = [
            f"Task: {todo['title']}",
            f"Description: {todo['description'] or 'N/A'}",
            f"Type: {todo['task_type']}",
        ]

        previous_run = intake_data.get("previous_run") if intake_data else None
        if previous_run:
            user_parts.append("\n## RETRY — Previous Run Context")
            user_parts.append(f"Previous state: {previous_run.get('previous_state', 'unknown')}")
            if previous_run.get("result_summary"):
                user_parts.append(f"Result summary: {previous_run['result_summary']}")

            prev_tasks = previous_run.get("sub_tasks", [])
            if prev_tasks:
                user_parts.append("\nPrevious sub-tasks:")
                for pst in prev_tasks:
                    status_icon = "✓" if pst["status"] == "completed" else "✗" if pst["status"] == "failed" else "○"
                    user_parts.append(f"  {status_icon} [{pst['role']}] {pst['title']} — {pst['status']}")
                    if pst.get("error"):
                        user_parts.append(f"    Error: {pst['error'][:300]}")

            git_diff = previous_run.get("git_diff")
            if git_diff:
                user_parts.append(f"\n## Existing Code Changes (git diff)\n{git_diff.get('stat', '')}")
                if git_diff.get("files"):
                    user_parts.append("\nChanged files:")
                    for f in git_diff["files"]:
                        user_parts.append(f"  {f['status']}\t{f['path']}")
                if git_diff.get("diff"):
                    user_parts.append(f"\nFull diff:\n```\n{git_diff['diff']}\n```")
                user_parts.append(
                    "\nIMPORTANT: The above code changes already exist in the workspace. "
                    "Create sub-tasks that build on or fix these existing changes — "
                    "do NOT start from scratch. Focus on what still needs to be done."
                )

            # Strip previous_run from intake_data to avoid duplication
            intake_for_prompt = {k: v for k, v in intake_data.items() if k != "previous_run"}
        else:
            intake_for_prompt = intake_data

        user_parts.append(f"\nIntake data: {json.dumps(intake_for_prompt, default=str)}")
        user_parts.append(f"Project context: {json.dumps(context, default=str)}")
        user_parts.append(
            "\nExplore the codebase first, then output your execution plan as a JSON object. "
            "The JSON must be the LAST thing you output — after all tool calls and exploration."
        )

        messages = [
            LLMMessage(role="system", content=planner_prompt),
            LLMMessage(role="user", content="\n".join(user_parts)),
        ]

        # Set up workspace tools for codebase exploration during planning
        planner_tools = None
        if workspace_path:
            planner_tools = self._get_builtin_tools(workspace_path, "planner")
            # Inject shared per-project index directory for semantic_search
            _plan_idx = os.path.normpath(os.path.join(workspace_path, "..", ".agent_index"))
            for _bt in planner_tools:
                if _bt["name"] == "semantic_search":
                    _bt["_index_dir"] = _plan_idx

            # Also include MCP tools if configured
            mcp_tools = await self.tools_registry.resolve_tools(
                project_id=str(todo["project_id"]),
                user_id=str(todo["creator_id"]),
            )
            if mcp_tools:
                existing_names = {t["name"] for t in planner_tools}
                planner_tools.extend(t for t in mcp_tools if t["name"] not in existing_names)

        plan = None
        max_retries = 3
        for attempt in range(max_retries):
            if planner_tools:
                # Use tool loop: planner explores codebase, then outputs JSON plan
                from agents.providers.base import run_tool_loop

                content, response = await run_tool_loop(
                    provider, messages,
                    tools=planner_tools,
                    tool_executor=lambda name, args: self.mcp_executor.execute_tool(
                        name, args, planner_tools,
                    ),
                    max_rounds=8,
                    on_activity=lambda msg: self._report_planning_activity(msg),
                    temperature=0.1,
                    max_tokens=16384,
                )
            else:
                # No workspace — simple LLM call
                response = await provider.send_message(messages, temperature=0.1, max_tokens=16384)
                content = response.content
            await self._track_tokens(response)

            # Try parsing the response as JSON (with trailing comma fix).
            # With tool loop, content may include exploration text + JSON at the end.
            # parse_llm_json handles extracting JSON from mixed content.
            plan = parse_llm_json(content)
            if plan is not None:
                logger.info(
                    "[%s] Plan parsed on attempt %d: keys=%s, sub_tasks=%d",
                    self.todo_id, attempt + 1, list(plan.keys()),
                    len(plan.get("sub_tasks", [])),
                )
                if plan.get("sub_tasks"):
                    for i, st in enumerate(plan["sub_tasks"]):
                        logger.info(
                            "[%s]   sub_task[%d]: role=%s title=%s deps=%s",
                            self.todo_id, i, st.get("agent_role"), st.get("title"),
                            st.get("depends_on", []),
                        )
                else:
                    logger.warning("[%s] Plan has ZERO sub_tasks! Raw content: %.500s",
                                   self.todo_id, content)
                break

            # Parse failed — log the raw LLM response for debugging
            content_len = len(content or "")
            logger.warning(
                "[%s] Plan parse attempt %d/%d failed. content_length=%d, "
                "stop_reason=%s, model=%s\nFull LLM response:\n%s",
                self.todo_id, attempt + 1, max_retries, content_len,
                response.stop_reason, response.model,
                content or "(empty)",
            )

            if attempt < max_retries - 1:
                # Retry with correction prompt (no tool loop, just JSON output)
                planner_tools = None
                messages = [
                    LLMMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
                    LLMMessage(
                        role="user",
                        content=(
                            f"Task: {todo['title']}\n"
                            f"Description: {todo['description'] or 'N/A'}\n"
                            f"Type: {todo['task_type']}\n"
                            f"Intake data: {json.dumps(intake_data, default=str)}\n"
                            f"Project context: {json.dumps(context, default=str)}\n\n"
                            "IMPORTANT: Your previous response was not valid JSON and could not be parsed. "
                            "You MUST respond with ONLY a valid JSON object. No markdown fences, no explanation, "
                            "no text before or after the JSON. Just the raw JSON object starting with {{ and ending with }}."
                        ),
                    ),
                ]
            else:
                # Final attempt failed — surface the raw response in chat for visibility
                truncated = (content or "")[-2000:]
                if content_len > 2000:
                    truncated = f"…(truncated, full length={content_len})\n{truncated}"
                await self._post_system_message(
                    f"**Planning failed** — could not parse plan after {max_retries} attempts.\n\n"
                    f"**Stop reason:** `{response.stop_reason}` | **Model:** `{response.model}` | "
                    f"**Response length:** {content_len} chars\n\n"
                    f"<details><summary>Raw LLM response (last 2000 chars)</summary>\n\n"
                    f"```\n{truncated}\n```\n</details>"
                )
                raise ValueError("Failed to parse execution plan from LLM after retries")

        # Store plan as structured JSON for human review
        await self.db.execute(
            "UPDATE todo_items SET plan_json = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            plan,
        )

        sub_tasks_text = "\n".join(
            f"  {i+1}. [{st['agent_role']}] {st['title']}"
            for i, st in enumerate(plan.get("sub_tasks", []))
        )
        await self._post_system_message(
            f"**Execution Plan:**\n\n{plan.get('summary', 'No summary')}\n\n"
            f"**Sub-tasks:** {len(plan.get('sub_tasks', []))}\n"
            + sub_tasks_text
            + "\n\nAuto-approved. Starting execution."
        )

        # Auto-approve: create sub-tasks and transition to execution
        await self._auto_approve_plan(todo, plan)

    async def _auto_approve_plan(self, todo: dict, plan: dict) -> None:
        """Create sub-tasks from plan and transition directly to execution."""
        logger.info("[%s] _auto_approve_plan: plan keys=%s, sub_tasks=%d",
                    self.todo_id, list(plan.keys()), len(plan.get("sub_tasks", [])))
        if not plan.get("sub_tasks"):
            logger.error("[%s] _auto_approve_plan: NO sub_tasks in plan! Plan: %.1000s",
                         self.todo_id, json.dumps(plan, default=str))
        sub_task_ids = []
        plan_index_to_id: dict[int, str] = {}  # plan index → DB id for dependency resolution
        first_chain_id = None  # track chain for review_loop sub-tasks
        for i, st in enumerate(plan.get("sub_tasks", [])):
            target_repo = st.get("target_repo")
            logger.info("[%s] Inserting sub_task[%d]: role=%s title=%s order=%s deps=%s review_loop=%s",
                        self.todo_id, i, st.get("agent_role"), st.get("title"),
                        st.get("execution_order", 0), st.get("depends_on", []),
                        st.get("review_loop", False))
            try:
                row = await self.db.fetchrow(
                    """
                    INSERT INTO sub_tasks (
                        todo_id, title, description, agent_role,
                        execution_order, input_context,
                        review_loop, target_repo
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    self.todo_id,
                    st["title"],
                    st.get("description", ""),
                    st["agent_role"],
                    st.get("execution_order", 0),
                    st.get("context", {}),
                    bool(st.get("review_loop", False)),
                    target_repo,
                )
                st_id = str(row["id"])
                sub_task_ids.append(st_id)
                # Map plan index → DB id for dependency resolution
                plan_index_to_id[i] = st_id
                logger.info("[%s] Inserted sub_task[%d] id=%s", self.todo_id, i, st_id)

                # For review_loop sub-tasks, set review_chain_id to themselves (chain root)
                if st.get("review_loop"):
                    await self.db.execute(
                        "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                        row["id"],
                    )
            except Exception:
                logger.exception("[%s] FAILED to insert sub_task[%d]: %s",
                                 self.todo_id, i, st.get("title"))

        # Set up dependencies using plan index mapping (resilient to failed inserts)
        for i, st in enumerate(plan.get("sub_tasks", [])):
            if i not in plan_index_to_id:
                continue  # This sub-task failed to insert
            depends_on = st.get("depends_on", [])
            if depends_on:
                dep_ids = [plan_index_to_id[j] for j in depends_on if j in plan_index_to_id]
                if dep_ids:
                    await self.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        plan_index_to_id[i],
                        dep_ids,
                    )

        logger.info("[%s] Created %d sub-tasks (ids=%s), transitioning to in_progress/executing",
                    self.todo_id, len(sub_task_ids), sub_task_ids)
        result = await self._transition_todo( "in_progress", sub_state="executing")
        if result:
            logger.info("[%s] Transitioned to in_progress successfully (state=%s, sub_state=%s)",
                        self.todo_id, result.get("state"), result.get("sub_state"))
        else:
            logger.error("[%s] FAILED to transition to in_progress! Optimistic lock failed or invalid state",
                         self.todo_id)

        # Notify human that execution has started
        await self.notifier.notify(
            str(todo["creator_id"]),
            "in_progress",
            {
                "todo_id": self.todo_id,
                "title": todo["title"],
                "detail": f"Auto-approved plan with {len(sub_task_ids)} sub-tasks. Executing.",
            },
        )

        # Immediately start execution
        logger.info("[%s] Resolving provider for execution...", self.todo_id)
        todo = await self._load_todo()
        provider = await self.provider_registry.resolve_for_todo(self.todo_id)
        logger.info("[%s] Provider resolved: %s/%s. Starting _phase_execution",
                    self.todo_id, provider.provider_type, provider.default_model)
        await self._phase_execution(todo, provider)

    # ---- EXECUTION PHASE ----

    async def _phase_execution(self, todo: dict, provider: AIProvider) -> None:
        """Execute sub-tasks in parallel, respecting dependencies."""
        logger.info("[%s] _phase_execution started (todo state=%s, sub_state=%s)",
                    self.todo_id, todo.get("state"), todo.get("sub_state"))
        sub_tasks = await self.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
            self.todo_id,
        )
        logger.info("[%s] Found %d sub-tasks in DB", self.todo_id, len(sub_tasks))
        for st in sub_tasks:
            logger.info("[%s]   sub_task id=%s status=%s role=%s title=%s deps=%s",
                        self.todo_id, st["id"], st["status"], st["agent_role"],
                        st["title"], st["depends_on"])
        if not sub_tasks:
            # No sub-tasks — skip to review
            logger.warning("[%s] No sub-tasks found in DB! Transitioning to review", self.todo_id)
            await self._transition_todo( "review")
            return

        # Reset stale sub-tasks from previous crashed coordinator runs.
        # If we hold the todo-level lock, no other coordinator is executing these,
        # so any assigned/running sub-tasks are guaranteed orphans.
        stale_statuses = ("assigned", "running")
        stale = [st for st in sub_tasks if st["status"] in stale_statuses]
        if stale:
            logger.warning("[%s] Resetting %d stale sub-tasks (assigned/running → pending): %s",
                           self.todo_id, len(stale),
                           [(str(s["id"])[:8], s["status"], s["agent_role"]) for s in stale])
            for st in stale:
                await self.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', updated_at = NOW() WHERE id = $1",
                    st["id"],
                )
            # Re-fetch after reset
            sub_tasks = await self.db.fetch(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
                self.todo_id,
            )

        # Set up task workspace if project has a repo
        workspace_path = None
        try:
            project = await self.db.fetchrow(
                "SELECT repo_url FROM projects WHERE id = $1", todo["project_id"]
            )
            if project and project.get("repo_url"):
                logger.info("[%s] Setting up workspace for repo %s", self.todo_id, project["repo_url"])
                workspace_path = await self.workspace_mgr.setup_task_workspace(self.todo_id)
                logger.info("[%s] Task workspace ready at %s", self.todo_id, workspace_path)
            else:
                logger.info("[%s] No repo_url on project, skipping workspace setup", self.todo_id)
        except Exception:
            logger.warning("Could not set up task workspace for %s", self.todo_id, exc_info=True)

        # Find runnable sub-tasks (pending + all dependencies completed)
        runnable = []
        all_st_ids = {str(s["id"]) for s in sub_tasks}
        for st in sub_tasks:
            if st["status"] != "pending":
                logger.info("[%s] Sub-task %s status=%s, skipping", self.todo_id, st["id"], st["status"])
                continue
            deps = st["depends_on"] or []
            if deps:
                # Detect broken deps: self-references or refs to non-existent subtasks
                st_id_str = str(st["id"])
                broken = [str(d) for d in deps if str(d) == st_id_str or str(d) not in all_st_ids]
                if broken:
                    logger.warning(
                        "[%s] Sub-task %s (%s) has broken depends_on refs: %s — clearing them",
                        self.todo_id, st["id"], st["title"], broken,
                    )
                    valid_deps = [d for d in deps if str(d) != st_id_str and str(d) in all_st_ids]
                    await self.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        st["id"],
                        valid_deps if valid_deps else [],
                    )
                    deps = valid_deps

                if deps:
                    dep_statuses = await self.db.fetch(
                        "SELECT id, status, title FROM sub_tasks WHERE id = ANY($1)",
                        deps,
                    )
                    unmet = [d for d in dep_statuses if d["status"] != "completed"]
                    if unmet:
                        unmet_detail = [(str(d["id"])[:8], d["status"], d["title"]) for d in unmet]
                        logger.info(
                            "[%s] Sub-task %s (%s) blocked on %d unmet deps: %s",
                            self.todo_id, st["id"], st["title"], len(unmet), unmet_detail,
                        )
                        continue
            runnable.append(dict(st))

        logger.info("[%s] %d runnable sub-tasks: %s", self.todo_id, len(runnable),
                    [(r["id"], r["agent_role"], r["title"]) for r in runnable])

        # Check for cancellation before dispatching
        if await self._is_cancelled():
            logger.info("[%s] Task cancelled before dispatch, aborting execution", self.todo_id)
            return

        if not runnable:
            # Check if all done
            logger.warning("[%s] No runnable sub-tasks found (total=%d). Checking if all done...",
                           self.todo_id, len(sub_tasks))
            all_done, any_failed = await check_all_subtasks_done(self.db, self.todo_id)
            logger.info("[%s] all_done=%s, any_failed=%s", self.todo_id, all_done, any_failed)
            if all_done:
                if any_failed:
                    failed_tasks = await self.db.fetch(
                        "SELECT title, error_message FROM sub_tasks "
                        "WHERE todo_id = $1 AND status = 'failed'",
                        self.todo_id,
                    )
                    err_detail = "; ".join(
                        f"{f['title']}: {f['error_message'] or 'unknown'}" for f in failed_tasks
                    )
                    await self._transition_todo(
                        "failed",
                        error_message=f"Sub-tasks failed: {err_detail}",
                    )
                else:
                    # All done — run testing then review
                    await self._enter_testing_or_review(todo, provider)
            # else: some tasks still running, will be picked up next cycle
            return

        # Resolve work rules for RALPH loop
        work_rules = await self._resolve_work_rules(todo)
        has_quality_rules = bool(work_rules.get("quality"))
        max_iterations = todo.get("max_iterations") or 50

        # Execute runnable sub-tasks in parallel
        logger.info("[%s] Dispatching %d sub-tasks (quality_rules=%s, max_iter=%d, workspace=%s)",
                    self.todo_id, len(runnable), has_quality_rules, max_iterations, workspace_path)
        tasks = []
        workspace_map: dict[str, str] = {}  # subtask_id → resolved workspace
        for st in runnable:
            # Resolve per-subtask workspace (dep repos get their own)
            st_workspace = workspace_path
            if st.get("target_repo"):
                try:
                    st_workspace = await self._setup_dependency_workspace(st)
                    logger.info("[%s] Dep workspace for %s: %s", self.todo_id, st["id"], st_workspace)
                except Exception:
                    logger.warning(
                        "Failed to set up dep workspace for %s", st["id"], exc_info=True,
                    )
            workspace_map[str(st["id"])] = st_workspace

            if st["agent_role"] == "merge_agent":
                logger.info("[%s] Sub-task %s: merge_agent path", self.todo_id, st["id"])
                tasks.append(self._execute_merge_subtask(st, provider, st_workspace))
            elif st["agent_role"] == "pr_creator":
                logger.info("[%s] Sub-task %s: pr_creator path", self.todo_id, st["id"])
                tasks.append(self._execute_pr_creator_subtask(st, provider, st_workspace))
            elif has_quality_rules:
                logger.info("[%s] Sub-task %s: iteration path (role=%s)", self.todo_id, st["id"], st["agent_role"])
                tasks.append(self._execute_subtask_with_iterations(
                    st, provider,
                    workspace_path=st_workspace,
                    work_rules=work_rules,
                    max_iterations=max_iterations,
                ))
            else:
                logger.info("[%s] Sub-task %s: simple execution path (role=%s)", self.todo_id, st["id"], st["agent_role"])
                tasks.append(self._execute_subtask(st, provider, workspace_path=st_workspace))

        logger.info("[%s] Waiting on %d sub-task coroutines...", self.todo_id, len(tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[%s] All sub-tasks returned. Processing results...", self.todo_id)

        for st, result in zip(runnable, results):
            if isinstance(result, Exception):
                logger.error("Sub-task %s failed: %s", st["id"], result, exc_info=result)
                error_msg = f"{type(result).__name__}: {result}"
                if len(error_msg) > 1000:
                    error_msg = error_msg[:1000] + "..."
                await self._transition_subtask(
                    str(st["id"]),
                    "failed",
                    error_message=error_msg,
                )
            else:
                # Handle review loop: create follow-up sub-tasks
                st_ws = workspace_map.get(str(st["id"]), workspace_path)
                await self._handle_subtask_completion(st, provider, st_ws)

        # Check user messages for steering
        user_msg = await self._check_for_user_messages()
        if user_msg:
            await self._handle_user_message(user_msg, provider)

        # Check for cancellation after batch
        if await self._is_cancelled():
            logger.info("[%s] Task cancelled after batch, aborting execution", self.todo_id)
            return

        # Re-scan for newly unblocked subtasks — completions above may have
        # unlocked dependents. Loop until no more runnable or all done.
        for rescan_round in range(1, 20):  # safety cap
            all_done, any_failed = await check_all_subtasks_done(self.db, self.todo_id)
            if all_done:
                break

            # Fetch fresh subtask list and find newly runnable
            fresh_sub_tasks = await self.db.fetch(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order",
                self.todo_id,
            )
            next_runnable = []
            for st2 in fresh_sub_tasks:
                if st2["status"] != "pending":
                    continue
                deps2 = st2["depends_on"] or []
                if deps2:
                    dep_sts = await self.db.fetch(
                        "SELECT id, status FROM sub_tasks WHERE id = ANY($1)", deps2,
                    )
                    if any(d["status"] != "completed" for d in dep_sts):
                        continue
                next_runnable.append(dict(st2))

            if not next_runnable:
                logger.info("[%s] Re-scan round %d: no more runnable subtasks", self.todo_id, rescan_round)
                break

            # Check for cancellation between re-scan rounds
            if await self._is_cancelled():
                logger.info("[%s] Task cancelled during re-scan, aborting", self.todo_id)
                return

            logger.info("[%s] Re-scan round %d: found %d newly runnable subtasks",
                        self.todo_id, rescan_round, len(next_runnable))
            next_tasks = []
            for st2 in next_runnable:
                st2_ws = workspace_path
                if st2.get("target_repo"):
                    try:
                        st2_ws = await self._setup_dependency_workspace(st2)
                    except Exception:
                        pass
                workspace_map[str(st2["id"])] = st2_ws

                if st2["agent_role"] == "merge_agent":
                    next_tasks.append(self._execute_merge_subtask(st2, provider, st2_ws))
                elif st2["agent_role"] == "pr_creator":
                    next_tasks.append(self._execute_pr_creator_subtask(st2, provider, st2_ws))
                elif has_quality_rules:
                    next_tasks.append(self._execute_subtask_with_iterations(
                        st2, provider, workspace_path=st2_ws,
                        work_rules=work_rules, max_iterations=max_iterations,
                    ))
                else:
                    next_tasks.append(self._execute_subtask(st2, provider, workspace_path=st2_ws))

            rescan_results = await asyncio.gather(*next_tasks, return_exceptions=True)
            for st2, result2 in zip(next_runnable, rescan_results):
                if isinstance(result2, Exception):
                    logger.error("Sub-task %s failed: %s", st2["id"], result2, exc_info=result2)
                    await self._transition_subtask(str(st2["id"]), "failed", error_message=str(result2))
                else:
                    st2_ws = workspace_map.get(str(st2["id"]), workspace_path)
                    await self._handle_subtask_completion(st2, provider, st2_ws)

        # Final check
        all_done, any_failed = await check_all_subtasks_done(self.db, self.todo_id)
        if all_done and not any_failed:
            # Guardrail: ensure coding tasks have test + review subtasks
            guardrail_created = await self._ensure_coding_guardrails(workspace_path)
            if guardrail_created:
                # New subtasks were created — execute them before review
                logger.info("[%s] Guardrail subtasks created, executing them", self.todo_id)
                guardrail_tasks = await self.db.fetch(
                    "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'pending' "
                    "ORDER BY execution_order, created_at",
                    self.todo_id,
                )
                for gst in guardrail_tasks:
                    deps = gst["depends_on"] or []
                    if deps:
                        dep_sts = await self.db.fetch(
                            "SELECT id, status FROM sub_tasks WHERE id = ANY($1)", deps,
                        )
                        if any(d["status"] != "completed" for d in dep_sts):
                            continue
                    gst_ws = workspace_path
                    if gst.get("target_repo"):
                        try:
                            gst_ws = await self._setup_dependency_workspace(gst)
                        except Exception:
                            pass
                    if has_quality_rules:
                        await self._execute_subtask_with_iterations(
                            dict(gst), provider,
                            workspace_path=gst_ws,
                            work_rules=work_rules,
                            max_iterations=max_iterations,
                        )
                    else:
                        await self._execute_subtask(dict(gst), provider, workspace_path=gst_ws)

                # Re-scan: guardrail subtasks may have unblocked others (reviewer after tester)
                for gr_round in range(1, 10):
                    gr_fresh = await self.db.fetch(
                        "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'pending' "
                        "ORDER BY execution_order, created_at",
                        self.todo_id,
                    )
                    gr_runnable = []
                    for gst2 in gr_fresh:
                        deps2 = gst2["depends_on"] or []
                        if deps2:
                            dep_sts2 = await self.db.fetch(
                                "SELECT id, status FROM sub_tasks WHERE id = ANY($1)", deps2,
                            )
                            if any(d["status"] != "completed" for d in dep_sts2):
                                continue
                        gr_runnable.append(dict(gst2))
                    if not gr_runnable:
                        break
                    logger.info("[%s] Guardrail re-scan round %d: %d runnable",
                                self.todo_id, gr_round, len(gr_runnable))
                    for gst2 in gr_runnable:
                        gst2_ws = workspace_path
                        if has_quality_rules:
                            await self._execute_subtask_with_iterations(
                                gst2, provider,
                                workspace_path=gst2_ws,
                                work_rules=work_rules,
                                max_iterations=max_iterations,
                            )
                        else:
                            await self._execute_subtask(gst2, provider, workspace_path=gst2_ws)

                # After guardrail execution, re-check completion
                all_done, any_failed = await check_all_subtasks_done(self.db, self.todo_id)
                if all_done and not any_failed:
                    code_push_created = await self._ensure_code_push(workspace_path)
                    if code_push_created:
                        pr_st = await self.db.fetchrow(
                            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
                            "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                            self.todo_id,
                        )
                        if pr_st:
                            await self._execute_pr_creator_subtask(dict(pr_st), provider, workspace_path)
                        # pr_creator may have created a merge_agent — execute it too
                        merge_st = await self.db.fetchrow(
                            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'merge_agent' "
                            "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                            self.todo_id,
                        )
                        if merge_st:
                            await self._execute_merge_subtask(dict(merge_st), provider, workspace_path)
                    await self._enter_testing_or_review(todo, provider)
                elif all_done and any_failed:
                    failed_tasks = await self.db.fetch(
                        "SELECT title, error_message FROM sub_tasks "
                        "WHERE todo_id = $1 AND status = 'failed'",
                        self.todo_id,
                    )
                    err = "; ".join(f"{f['title']}: {f['error_message']}" for f in failed_tasks)
                    await self._transition_todo( "failed", error_message=err)
            else:
                code_push_created = await self._ensure_code_push(workspace_path)
                if code_push_created:
                    pr_st = await self.db.fetchrow(
                        "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
                        "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                        self.todo_id,
                    )
                    if pr_st:
                        await self._execute_pr_creator_subtask(dict(pr_st), provider, workspace_path)
                    # pr_creator may have created a merge_agent — execute it too
                    merge_st = await self.db.fetchrow(
                        "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'merge_agent' "
                        "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                        self.todo_id,
                    )
                    if merge_st:
                        await self._execute_merge_subtask(dict(merge_st), provider, workspace_path)
                await self._enter_testing_or_review(todo, provider)
        elif all_done and any_failed:
            # Check if we should retry
            if todo["retry_count"] < todo["max_retries"]:
                await self.db.execute(
                    "UPDATE todo_items SET retry_count = retry_count + 1 WHERE id = $1",
                    self.todo_id,
                )
                # Reset failed sub-tasks to pending
                await self.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', retry_count = retry_count + 1 "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    self.todo_id,
                )
                await self._post_system_message("**Retrying failed sub-tasks...**")
            else:
                failed_tasks = await self.db.fetch(
                    "SELECT title, error_message FROM sub_tasks "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    self.todo_id,
                )
                err = "; ".join(f"{f['title']}: {f['error_message']}" for f in failed_tasks)
                await self._transition_todo( "failed", error_message=err)

    # ---- WORK RULES ----

    async def _resolve_work_rules(self, todo: dict) -> dict:
        """Merge project-level work rules with task-level overrides.

        Returns a dict like {"coding": [...], "testing": [...], ...}.
        Task overrides replace project rules per-category.
        """
        project = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", todo["project_id"]
        )
        project_settings = project["settings_json"] or {} if project else {}
        if isinstance(project_settings, str):
            project_settings = json.loads(project_settings)
        rules = dict(project_settings.get("work_rules", {}))

        # Task-level overrides
        overrides = todo.get("rules_override_json") or {}
        if isinstance(overrides, str):
            overrides = json.loads(overrides)
        for category, values in overrides.items():
            rules[category] = values

        return rules

    def _filter_rules_for_role(self, work_rules: dict, role: str) -> dict:
        """Return only the rule categories relevant to the given agent role."""
        defn = get_agent_definition(role)
        categories = defn.tool_rule_categories if defn else ["general"]
        return {cat: work_rules[cat] for cat in categories if cat in work_rules}

    def _format_rules_for_prompt(self, rules: dict) -> str:
        """Format work rules into a string block for injection into prompts."""
        if not rules:
            return ""
        parts = ["\n\n## Work Rules (you MUST follow these)\n"]
        for category, items in rules.items():
            if items:
                parts.append(f"### {category.title()}")
                for item in items:
                    parts.append(f"- {item}")
        return "\n".join(parts)

    # ---- TESTING PHASE ----

    TESTING_SYSTEM_PROMPT = (
        "You are a build and test verification agent. Your job is to verify that the implemented "
        "code changes can be built, dependencies are installed, and all tests pass.\n\n"
        "## Workflow\n"
        "1. Install dependencies (npm install, pip install, cargo build, etc.)\n"
        "2. Run build commands to ensure the project compiles/builds\n"
        "3. Run the full test suite\n"
        "4. Report results with specific pass/fail details\n\n"
        "## Rules\n"
        "- Run ALL provided commands; do not skip any\n"
        "- If a command fails, capture the full error output\n"
        "- Do not attempt to fix code -- only report results\n"
        "- Be thorough: check for missing dependencies, broken imports, type errors\n"
    )

    _MAX_TEST_RETRIES = 2  # Max times to loop back for test fixes

    async def _enter_testing_or_review(self, todo: dict, provider: AIProvider) -> None:
        """Route to testing phase if code work was done, otherwise skip to review."""
        coder_count = await self.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
            self.todo_id,
        )
        if coder_count and coder_count > 0:
            logger.info(
                "[%s] Code work detected (%d coder subtasks), entering testing phase",
                self.todo_id, coder_count,
            )
            await self._phase_testing(todo, provider)
        else:
            logger.info("[%s] No coder work detected, skipping testing → review", self.todo_id)
            await self._phase_review(todo, provider)

    async def _phase_testing(self, todo: dict, provider: AIProvider) -> None:
        """Dedicated testing phase: install deps, build, run tests.

        Transitions to review on success, back to in_progress if fixes needed.
        """
        # Transition to testing state
        current = await self.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", self.todo_id,
        )
        if current == "testing":
            logger.info("[%s] Already in testing state", self.todo_id)
        elif current == "in_progress":
            await self._transition_todo("testing", sub_state="build_and_test")
        else:
            logger.warning("[%s] Cannot enter testing from state=%s", self.todo_id, current)
            return

        await self._post_system_message(
            "**Entering testing phase.** Installing dependencies, building, and running tests..."
        )

        # 1. Resolve workspace
        workspace_path = None
        try:
            project = await self.db.fetchrow(
                "SELECT repo_url FROM projects WHERE id = $1", todo["project_id"]
            )
            if project and project.get("repo_url"):
                workspace_path = await self.workspace_mgr.setup_task_workspace(self.todo_id)
        except Exception:
            logger.warning("[%s] Could not set up workspace for testing", self.todo_id, exc_info=True)

        if not workspace_path:
            logger.info("[%s] No workspace for testing, skipping to review", self.todo_id)
            todo = await self._load_todo()
            await self._phase_review(todo, provider)
            return

        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        # 2. Resolve build/test commands from project settings and work rules
        work_rules = await self._resolve_work_rules(todo)
        project_settings = await self._get_project_settings(todo)

        build_commands = project_settings.get("build_commands", [])
        quality_commands = work_rules.get("quality", [])
        testing_rules = work_rules.get("testing", [])

        has_explicit_commands = bool(build_commands or quality_commands or testing_rules)

        # 3. Run tests
        if has_explicit_commands:
            test_results = await self._run_testing_commands(
                repo_dir, build_commands, quality_commands, testing_rules,
            )
        else:
            test_results = await self._run_testing_with_discovery(
                todo, provider, workspace_path,
            )

        # 4. Evaluate results
        if test_results["passed"]:
            summary = test_results.get("summary", "All checks passed.")
            await self._post_system_message(
                f"**Testing passed.** {summary}\n\nProceeding to review."
            )
            await self.db.execute(
                "UPDATE todo_items SET sub_state = 'tests_passed', updated_at = NOW() WHERE id = $1",
                self.todo_id,
            )
            todo = await self._load_todo()
            await self._phase_review(todo, provider)
        else:
            # Check retry count
            retry_count = todo.get("retry_count", 0)
            if retry_count < self._MAX_TEST_RETRIES:
                await self._create_test_fix_subtasks(
                    todo, provider, test_results, workspace_path,
                )
                await self._transition_todo(
                    "in_progress", sub_state="fixing_test_failures",
                )
                todo = await self._load_todo()
                await self._phase_execution(todo, provider)
            else:
                error_output = test_results.get("error_output", "Unknown failures")
                await self._post_system_message(
                    f"**Testing failed after {retry_count + 1} attempts.**\n\n"
                    f"Failures:\n```\n{error_output[:1000]}\n```\n\n"
                    "Proceeding to review with known test failures."
                )
                todo = await self._load_todo()
                await self._phase_review(todo, provider)

    async def _run_testing_commands(
        self,
        repo_dir: str,
        build_commands: list[str],
        quality_commands: list[str],
        testing_rules: list[str],
    ) -> dict:
        """Run explicit build/test commands and return results.

        Returns {"passed": bool, "summary": str, "error_output": str | None, "steps": list}.
        """
        steps: list[dict] = []
        all_passed = True

        # Phase 1: Install dependencies (auto-detect)
        dep_install_cmds = self._detect_dependency_install_commands(repo_dir)
        for cmd in dep_install_cmds:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir, timeout=180)
                passed = exit_code == 0
                steps.append({"command": cmd, "passed": passed, "output": output[:2000], "phase": "install"})
                if not passed:
                    all_passed = False
                await self._publish_testing_progress(cmd, passed)
            except Exception as e:
                steps.append({"command": cmd, "passed": False, "output": str(e)[:500], "phase": "install"})
                all_passed = False
                await self._publish_testing_progress(cmd, False)

        # Phase 2: Build commands (from project settings)
        for cmd in build_commands:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir, timeout=120)
                passed = exit_code == 0
                steps.append({"command": cmd, "passed": passed, "output": output[:2000], "phase": "build"})
                if not passed:
                    all_passed = False
                await self._publish_testing_progress(cmd, passed)
            except Exception as e:
                steps.append({"command": cmd, "passed": False, "output": str(e)[:500], "phase": "build"})
                all_passed = False
                await self._publish_testing_progress(cmd, False)

        # Phase 3: Quality checks (from work_rules.quality)
        for cmd in quality_commands:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir, timeout=120)
                passed = exit_code == 0
                steps.append({"command": cmd, "passed": passed, "output": output[:2000], "phase": "quality"})
                if not passed:
                    all_passed = False
                await self._publish_testing_progress(cmd, passed)
            except Exception as e:
                steps.append({"command": cmd, "passed": False, "output": str(e)[:500], "phase": "quality"})
                all_passed = False
                await self._publish_testing_progress(cmd, False)

        # Phase 4: Testing rules (from work_rules.testing)
        for cmd in testing_rules:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir, timeout=180)
                passed = exit_code == 0
                steps.append({"command": cmd, "passed": passed, "output": output[:2000], "phase": "test"})
                if not passed:
                    all_passed = False
                await self._publish_testing_progress(cmd, passed)
            except Exception as e:
                steps.append({"command": cmd, "passed": False, "output": str(e)[:500], "phase": "test"})
                all_passed = False
                await self._publish_testing_progress(cmd, False)

        failed_steps = [s for s in steps if not s["passed"]]
        error_output = "\n".join(
            f"[FAIL] {s['command']}:\n{s['output']}" for s in failed_steps
        ) if failed_steps else None

        summary = f"{len(steps)} commands run, {len(failed_steps)} failed" if steps else "No commands to run"
        return {
            "passed": all_passed,
            "summary": summary,
            "error_output": error_output,
            "steps": steps,
        }

    @staticmethod
    def _detect_dependency_install_commands(repo_dir: str) -> list[str]:
        """Auto-detect dependency installation commands from the repo."""
        commands: list[str] = []

        # Node.js
        package_json = os.path.join(repo_dir, "package.json")
        if os.path.isfile(package_json):
            pnpm_lock = os.path.join(repo_dir, "pnpm-lock.yaml")
            yarn_lock = os.path.join(repo_dir, "yarn.lock")
            lock_file = os.path.join(repo_dir, "package-lock.json")
            if os.path.isfile(pnpm_lock):
                commands.append("pnpm install --frozen-lockfile")
            elif os.path.isfile(yarn_lock):
                commands.append("yarn install --frozen-lockfile")
            elif os.path.isfile(lock_file):
                commands.append("npm ci")
            else:
                commands.append("npm install")

        # Python
        requirements = os.path.join(repo_dir, "requirements.txt")
        pyproject = os.path.join(repo_dir, "pyproject.toml")
        if os.path.isfile(requirements):
            commands.append("pip install -r requirements.txt")
        elif os.path.isfile(pyproject):
            commands.append("pip install -e .")

        # Rust
        cargo_toml = os.path.join(repo_dir, "Cargo.toml")
        if os.path.isfile(cargo_toml):
            commands.append("cargo build")

        # Go
        go_mod = os.path.join(repo_dir, "go.mod")
        if os.path.isfile(go_mod):
            commands.append("go mod download")

        return commands

    async def _run_testing_with_discovery(
        self, todo: dict, provider: AIProvider, workspace_path: str,
    ) -> dict:
        """Use an LLM agent to discover and run build/test commands."""
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        # Install detected dependencies first
        dep_cmds = self._detect_dependency_install_commands(repo_dir)
        for cmd in dep_cmds:
            try:
                await self.workspace_mgr.run_command(cmd, repo_dir, timeout=180)
                await self._publish_testing_progress(cmd, True)
            except Exception:
                logger.warning("[%s] Dep install failed: %s", self.todo_id, cmd, exc_info=True)
                await self._publish_testing_progress(cmd, False)

        # Build system prompt for LLM-driven test discovery
        tester_context = await self._build_tester_context(todo)
        system_prompt = self.TESTING_SYSTEM_PROMPT + tester_context
        system_prompt += build_tools_prompt_block("tester")

        # Add file tree for orientation
        try:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=3)
            system_prompt += f"\n\nProject file structure:\n{file_tree}\n"
        except Exception:
            pass

        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=(
                "Discover and run all build and test commands for this project. "
                "Check package.json, pyproject.toml, Makefile, CI configs, etc. "
                "Install any missing dependencies, then run the commands and report results.\n\n"
                'You MUST output JSON at the end: {"passed": true/false, "summary": "...", '
                '"commands_run": ["cmd1", "cmd2"], "failures": ["failure detail"]}'
            )),
        ]

        tools = self._get_builtin_tools(workspace_path, "tester")

        try:
            from agents.providers.base import run_tool_loop

            content, response = await run_tool_loop(
                provider, messages,
                tools=tools,
                tool_executor=lambda name, args: self.mcp_executor.execute_tool(name, args, tools),
                max_rounds=10,
                temperature=0.1,
            )
            await self._track_tokens(response)

            result = parse_llm_json(content)
            if result is None:
                return {"passed": True, "summary": "Testing completed (could not parse LLM result)"}

            return {
                "passed": result.get("passed", True),
                "summary": result.get("summary", ""),
                "error_output": "\n".join(result.get("failures", [])) if result.get("failures") else None,
            }
        except Exception as e:
            logger.error("[%s] LLM test discovery failed: %s", self.todo_id, e, exc_info=True)
            return {
                "passed": True,
                "summary": f"Test discovery failed ({type(e).__name__}), skipping",
            }

    async def _create_test_fix_subtasks(
        self, todo: dict, provider: AIProvider, test_results: dict, workspace_path: str | None,
    ) -> None:
        """When testing fails, create coder subtask(s) to fix the issues."""
        error_output = test_results.get("error_output", "Unknown test failures")

        fix_description = (
            "The testing phase found failures that need to be fixed:\n\n"
            f"```\n{error_output[:3000]}\n```\n\n"
            "Investigate these failures and fix the underlying code issues. "
            "Do NOT disable tests or skip checks -- fix the actual problems."
        )

        await self._create_guardrail_subtask(
            title="Fix test failures from testing phase",
            description=fix_description,
            role="coder",
            depends_on=[],
        )

        # Increment retry count
        await self.db.execute(
            "UPDATE todo_items SET retry_count = retry_count + 1, updated_at = NOW() WHERE id = $1",
            self.todo_id,
        )

        await self._post_system_message(
            f"**Testing failed.** Creating fix subtask and retrying.\n\n"
            f"Failures:\n```\n{error_output[:500]}\n```"
        )

    async def _get_project_settings(self, todo: dict) -> dict:
        """Fetch and parse project settings_json."""
        project = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", todo["project_id"]
        )
        settings = (project.get("settings_json") or {}) if project else {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        return settings

    async def _publish_testing_progress(self, command: str, passed: bool) -> None:
        """Publish a testing progress event to the WebSocket channel."""
        try:
            status = "passed" if passed else "failed"
            await self.redis.publish(
                f"task:{self.todo_id}:events",
                json.dumps({
                    "type": "testing_step",
                    "command": command,
                    "status": status,
                }),
            )
        except Exception:
            pass

    # ---- RALPH ITERATION LOOP ----

    async def _execute_subtask_with_iterations(
        self,
        sub_task: dict,
        provider: AIProvider,
        *,
        workspace_path: str | None = None,
        work_rules: dict | None = None,
        max_iterations: int = 50,
    ) -> None:
        """RALPH-style iterative execution: fresh context per iteration,
        quality checks, stuck detection, hard cutoff."""
        st_id = str(sub_task["id"])
        logger.info("[%s] _execute_subtask_with_iterations START: st=%s role=%s title=%s workspace=%s max_iter=%d",
                    self.todo_id, st_id, sub_task["agent_role"], sub_task["title"], workspace_path, max_iterations)
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        await self.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            sub_task["agent_role"],
        )

        iteration_log: list[dict] = []
        unstuck_advice: str | None = None
        agent_signaled_done = False
        agent_done_summary: str | None = None
        qc_retries_after_done = 0
        _MAX_QC_RETRIES_AFTER_DONE = 2  # Max extra iterations when agent signals done but QC fails
        role = sub_task["agent_role"]
        role_rules = self._filter_rules_for_role(work_rules or {}, role)
        has_quality_rules = bool(role_rules.get("quality"))

        # Resolve custom agent config for this role
        todo_for_agent = await self._load_todo()
        agent_config = await self._resolve_agent_config(role, str(todo_for_agent["creator_id"]))
        # Resolution: agent config > task-level > provider default
        model_override = None
        if agent_config and agent_config.get("model_preference"):
            model_override = agent_config["model_preference"]
        elif todo_for_agent.get("ai_model"):
            model_override = todo_for_agent["ai_model"]

        # Load architect/editor config for this project
        _ae_project = await self.db.fetchrow(
            "SELECT architect_editor_enabled, architect_model, editor_model FROM projects WHERE id = $1",
            (await self._load_todo())["project_id"],
        )
        architect_editor_enabled = bool(_ae_project and _ae_project.get("architect_editor_enabled"))
        architect_model = (_ae_project or {}).get("architect_model")
        editor_model = (_ae_project or {}).get("editor_model")

        for iteration in range(1, max_iterations + 1):
            # Check for cancellation at the start of each iteration
            if await self._is_cancelled():
                logger.info("[%s] Task cancelled during iteration %d of %s, aborting",
                            self.todo_id, iteration, st_id)
                return

            start_time = time.monotonic()

            # Emit iteration_start event for streaming visibility
            try:
                await self.redis.publish(
                    f"task:{self.todo_id}:events",
                    json.dumps({
                        "type": "iteration_start",
                        "sub_task_id": st_id,
                        "iteration": iteration,
                        "subtask": sub_task["title"],
                    }),
                )
            except Exception:
                pass

            # Create agent run record
            run = await self.db.fetchrow(
                """
                INSERT INTO agent_runs (
                    todo_id, sub_task_id, agent_role, agent_model, provider_type
                )
                VALUES ($1, $2, $3, $4, $5) RETURNING *
                """,
                self.todo_id,
                sub_task["id"],
                role,
                provider.default_model,
                provider.provider_type,
            )

            try:
                await self._report_progress(
                    st_id,
                    min(10 + iteration * 2, 80),
                    f"Iteration {iteration}: {sub_task['title']}",
                )

                # 1. Build FRESH context (no carried conversation)
                context = await self._build_iteration_context(
                    sub_task=sub_task,
                    iteration=iteration,
                    iteration_log=iteration_log,
                    workspace_path=workspace_path,
                    work_rules=role_rules,
                    unstuck_advice=unstuck_advice,
                    agent_config=agent_config,
                )

                # Resolve MCP tools
                todo = await self._load_todo()
                mcp_tools = await self.tools_registry.resolve_tools(
                    project_id=str(todo["project_id"]),
                    user_id=str(todo["creator_id"]),
                )
                skills_ctx = await self.tools_registry.build_skills_context(
                    project_id=str(todo["project_id"]),
                    user_id=str(todo["creator_id"]),
                )

                # Filter tools per-agent
                if agent_config and agent_config.get("tools_enabled"):
                    allowed = set(agent_config["tools_enabled"])
                    if mcp_tools:
                        mcp_tools = [t for t in mcp_tools if t.get("name") in allowed]

                # Ensure agents always have workspace tools (built-in fallback)
                if workspace_path:
                    builtin_tools = self._get_builtin_tools(workspace_path, role)
                    # Inject shared per-project index directory for semantic_search
                    shared_index_dir = os.path.normpath(os.path.join(workspace_path, "..", ".agent_index"))
                    for bt in builtin_tools:
                        if bt["name"] == "semantic_search":
                            bt["_index_dir"] = shared_index_dir
                    if mcp_tools:
                        existing_names = {t["name"] for t in mcp_tools}
                        mcp_tools.extend(t for t in builtin_tools if t["name"] not in existing_names)
                    else:
                        mcp_tools = builtin_tools

                system_prompt = context["system"]
                if skills_ctx:
                    system_prompt += skills_ctx

                # 2. Execute single iteration (one LLM call + tool loop)
                messages = [
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=context["user"]),
                ]
                tools_arg = mcp_tools if mcp_tools else None

                from agents.providers.base import run_tool_loop

                send_kwargs: dict = {"temperature": 0.1, "max_tokens": 16384}
                if model_override:
                    send_kwargs["model"] = model_override

                async def _iter_tool_exec(name: str, args: dict) -> str:
                    nonlocal agent_signaled_done, agent_done_summary
                    if name == "task_complete":
                        agent_signaled_done = True
                        agent_done_summary = args.get("summary", "")
                        return "Task completion acknowledged. Wrapping up."
                    if name == "create_subtask":
                        return await self._handle_create_subtask_tool(
                            sub_task, args, workspace_path,
                        )
                    return await self.mcp_executor.execute_tool(name, args, mcp_tools)

                async def _on_tool_event(event: dict) -> None:
                    """Publish structured tool events for streaming execution visibility."""
                    try:
                        event["sub_task_id"] = st_id
                        await self.redis.publish(
                            f"task:{self.todo_id}:events",
                            json.dumps(event),
                        )
                    except Exception:
                        pass  # Don't let event publishing break execution

                # Architect/Editor dual-model execution
                if architect_editor_enabled and architect_model and editor_model:
                    READ_ONLY_TOOLS = {"read_file", "list_directory", "search_files", "semantic_search"}
                    WRITE_TOOLS = {"write_file", "edit_file", "run_command", "task_complete", "create_subtask"}

                    # Phase A: Architect — powerful model with read-only tools
                    architect_tools = [t for t in (tools_arg or []) if t["name"] in READ_ONLY_TOOLS]
                    architect_kwargs = {**send_kwargs, "model": architect_model}

                    async def _architect_tool_exec(name: str, args: dict) -> str:
                        return await self.mcp_executor.execute_tool(name, args, mcp_tools)

                    architect_content, architect_response = await run_tool_loop(
                        provider, list(messages),
                        tools=architect_tools or None,
                        tool_executor=_architect_tool_exec,
                        max_rounds=10,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] [Architect] {msg}"),
                        on_tool_event=_on_tool_event,
                        **architect_kwargs,
                    )

                    # Phase B: Editor — fast model with write tools, guided by architect's output
                    editor_tools = [t for t in (tools_arg or []) if t["name"] in WRITE_TOOLS]
                    editor_system = (
                        system_prompt
                        + "\n\n## Architect's Analysis & Plan\n"
                        + "Follow the architect's instructions below to make the required changes.\n\n"
                        + (architect_content or architect_response.content or "No architect output.")
                    )
                    editor_messages = [
                        LLMMessage(role="system", content=editor_system),
                        LLMMessage(role="user", content=(
                            "Apply the changes described in the architect's plan above. "
                            "Use the available write tools to implement the changes."
                        )),
                    ]
                    editor_kwargs = {**send_kwargs, "model": editor_model}

                    iter_content, response = await run_tool_loop(
                        provider, editor_messages,
                        tools=editor_tools or None,
                        tool_executor=_iter_tool_exec,
                        max_rounds=10,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] [Editor] {msg}"),
                        on_tool_event=_on_tool_event,
                        **editor_kwargs,
                    )

                    # Combine token usage from both phases
                    response.tokens_input += architect_response.tokens_input
                    response.tokens_output += architect_response.tokens_output
                    if response.cost_usd and architect_response.cost_usd:
                        response.cost_usd += architect_response.cost_usd
                else:
                    # Standard single-model execution
                    iter_content, response = await run_tool_loop(
                        provider, messages,
                        tools=tools_arg,
                        tool_executor=_iter_tool_exec,
                        max_rounds=10,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] {msg}"),
                        on_tool_event=_on_tool_event,
                        **send_kwargs,
                    )

                # Detect tool loop truncation — LLM wanted more tool calls but max_rounds hit
                tool_loop_truncated = response.stop_reason == "max_tool_rounds"
                if tool_loop_truncated:
                    logger.warning(
                        "[%s] Tool loop truncated at max_rounds for subtask %s iteration %d",
                        self.todo_id, st_id, iteration,
                    )

                # Broadcast the LLM's text response so the UI can display it
                if response.content and response.content.strip():
                    # Truncate for the activity stream (full content stored in iteration log)
                    preview = response.content.strip()
                    if len(preview) > 500:
                        preview = preview[:500] + "..."
                    await self.redis.publish(
                        f"task:{self.todo_id}:progress",
                        json.dumps({
                            "type": "llm_response",
                            "sub_task_id": st_id,
                            "iteration": iteration,
                            "content": response.content.strip(),
                            "preview": preview,
                        }),
                    )

                duration_ms = int((time.monotonic() - start_time) * 1000)
                total_tokens = response.tokens_input + response.tokens_output

                # Update agent run
                await self.db.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'completed', output_result = $2,
                        tokens_input = $3, tokens_output = $4,
                        duration_ms = $5, cost_usd = $6, completed_at = NOW()
                    WHERE id = $1
                    """,
                    run["id"],
                    {"content": response.content},
                    response.tokens_input,
                    response.tokens_output,
                    duration_ms,
                    response.cost_usd,
                )

                # Track tokens on TODO
                await self._track_tokens(response)

                # 3. Run quality checks (if quality rules exist)
                if has_quality_rules and workspace_path:
                    qc_result = await self._run_quality_checks(
                        workspace_path, role_rules, role,
                    )
                else:
                    qc_result = {"passed": True, "reason": "no quality rules", "learnings": []}

                # Report QC result as activity
                if has_quality_rules and workspace_path:
                    if qc_result["passed"]:
                        await self._report_activity(st_id, f"[iter {iteration}] Quality check passed")
                    else:
                        reason = qc_result.get("reason", "failed")
                        await self._report_activity(st_id, f"[iter {iteration}] Quality check failed: {reason}")

                # 4. Record iteration
                action = "implement" if iteration == 1 else ("fix_with_advice" if unstuck_advice else "fix")
                learnings = qc_result.get("learnings", [])
                if tool_loop_truncated:
                    learnings.append(
                        "Tool loop was truncated (hit max_rounds=10). "
                        "Agent may not have finished all intended tool calls."
                    )
                entry = {
                    "iteration": iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "outcome": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                    "error_output": qc_result.get("error_output") if not qc_result["passed"] else None,
                    "learnings": learnings,
                    "files_changed": [],
                    "stuck_check": None,
                    "tokens_used": total_tokens,
                    "tool_loop_truncated": tool_loop_truncated,
                    "llm_response": response.content.strip()[:2000] if response.content else None,
                }
                iteration_log.append(entry)

                # Persist iteration log
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                    sub_task["id"],
                    iteration_log,
                )

                # Emit iteration_end event for streaming visibility
                try:
                    await self.redis.publish(
                        f"task:{self.todo_id}:events",
                        json.dumps({
                            "type": "iteration_end",
                            "sub_task_id": st_id,
                            "iteration": iteration,
                            "status": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                        }),
                    )
                except Exception:
                    pass

                # 5. Quality checks passed -> validate output then DONE
                if qc_result["passed"]:
                    from agents.orchestrator.output_validator import (
                        validate_agent_output as _validate_output,
                        build_correction_prompt as _build_correction,
                        MAX_VALIDATION_RETRIES as _MAX_VAL_RETRIES,
                    )
                    validated_output = None
                    for val_attempt in range(_MAX_VAL_RETRIES + 1):
                        validated_output, val_errors = _validate_output(
                            role, response.content,
                        )
                        if validated_output is not None:
                            break
                        if val_attempt < _MAX_VAL_RETRIES:
                            correction = _build_correction(role, val_errors, response.content)
                            messages.append(LLMMessage(role="assistant", content=response.content))
                            messages.append(LLMMessage(role="user", content=correction))
                            response = await provider.send_message(
                                messages, temperature=0.1, tools=tools_arg,
                                **({"model": model_override} if model_override else {}),
                            )

                    if validated_output is None:
                        logger.warning(
                            "RALPH subtask %s (%s) failed output validation after %d retries: %s",
                            st_id, role, _MAX_VAL_RETRIES, val_errors,
                        )
                        validated_output = {
                            "content": response.content,
                            "raw_content": response.content,
                            "_validation_failed": True,
                            "_validation_errors": val_errors,
                        }

                    await self._transition_subtask(
                        st_id, "completed",
                        progress_pct=100,
                        progress_message="Done",
                        output_result=validated_output,
                    )
                    await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)
                    await self._append_progress_log(
                        sub_task, iteration, "completed", iteration_log,
                    )
                    await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")
                    return

                # 5b. Agent signaled done via task_complete
                if agent_signaled_done:
                    # If quality checks failed, don't accept — force the agent to fix
                    if not qc_result["passed"] and qc_retries_after_done < _MAX_QC_RETRIES_AFTER_DONE:
                        qc_retries_after_done += 1
                        logger.warning(
                            "[%s] Agent signaled task_complete on iteration %d but QC failed "
                            "(retry %d/%d): %s",
                            self.todo_id, iteration, qc_retries_after_done,
                            _MAX_QC_RETRIES_AFTER_DONE, qc_result.get("reason", "unknown"),
                        )
                        # Record the premature completion attempt in iteration log
                        entry["outcome"] = f"agent_done_but_qc_failed ({qc_result.get('reason', 'unknown')})"
                        entry["error_output"] = qc_result.get("error_output")
                        entry["learnings"].append(
                            "Agent signaled task_complete but quality checks did not pass. "
                            "Continuing iteration to fix quality issues."
                        )
                        iteration_log[-1] = entry
                        await self.db.execute(
                            "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                            sub_task["id"], iteration_log,
                        )
                        # Reset the signal so the loop continues
                        agent_signaled_done = False
                        agent_done_summary = ""
                        # Skip to next iteration — context builder will include QC failure
                        continue

                    # QC passed (or retries exhausted) — accept the result
                    if not qc_result["passed"]:
                        logger.warning(
                            "[%s] Agent signaled task_complete, QC still failing after %d retries — "
                            "accepting with warning",
                            self.todo_id, _MAX_QC_RETRIES_AFTER_DONE,
                        )
                    logger.info(
                        "[%s] Agent signaled task_complete on iteration %d (qc=%s)",
                        self.todo_id, iteration, "passed" if qc_result["passed"] else "failed_exhausted",
                    )

                    from agents.orchestrator.output_validator import (
                        validate_agent_output as _validate_output_done,
                    )
                    validated_output, _ = _validate_output_done(role, response.content)
                    if validated_output is None:
                        validated_output = {
                            "content": response.content,
                            "raw_content": response.content,
                            "summary": agent_done_summary,
                        }
                    else:
                        validated_output["summary"] = agent_done_summary

                    if not qc_result["passed"]:
                        validated_output["_qc_failed"] = True
                        validated_output["_qc_reason"] = qc_result.get("reason", "unknown")

                    await self._transition_subtask(
                        st_id, "completed",
                        progress_pct=100,
                        progress_message=agent_done_summary or "Done",
                        output_result=validated_output,
                    )
                    await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)
                    await self._append_progress_log(
                        sub_task, iteration, "completed_agent_signal", iteration_log,
                    )
                    await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")
                    return

                # Clear advice after use
                unstuck_advice = None

                # 6. Stuck detection every 15 iterations
                if iteration % 15 == 0 and iteration < max_iterations:
                    stuck_result = await self._check_if_stuck(iteration_log, provider)
                    entry["stuck_check"] = stuck_result
                    # Update the entry in log
                    iteration_log[-1] = entry
                    await self.db.execute(
                        "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                        sub_task["id"],
                        iteration_log,
                    )

                    if stuck_result.get("stuck"):
                        unstuck_advice = stuck_result.get("advice")
                        await self._report_activity(
                            st_id,
                            f"[iter {iteration}] Stuck detected: {stuck_result.get('pattern', 'loop')} — injecting advice",
                        )
                        await self._post_system_message(
                            f"**Stuck detection (iteration {iteration}):** {stuck_result.get('pattern', 'Loop detected')}\n\n"
                            f"Injecting supervisor advice for next iteration."
                        )

            except Exception as e:
                import traceback as _tb

                duration_ms = int((time.monotonic() - start_time) * 1000)
                error_type = _classify_error(e)
                error_detail = f"{type(e).__name__}: {e}"
                error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

                await self.db.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', error_type = $2,
                        error_detail = $3, duration_ms = $4, completed_at = NOW()
                    WHERE id = $1
                    """,
                    run["id"],
                    error_type,
                    error_detail + "\n\n" + error_traceback[-2000:],
                    duration_ms,
                )
                # Record failure in iteration log with full context
                iteration_log.append({
                    "iteration": iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "error",
                    "outcome": f"exception: {error_detail[:500]}",
                    "error_output": error_traceback[-3000:],
                    "learnings": [],
                    "tokens_used": 0,
                })
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                    sub_task["id"],
                    iteration_log,
                )
                # Report the error as activity so it appears in the UI immediately
                await self.redis.publish(
                    f"task:{self.todo_id}:progress",
                    json.dumps({
                        "type": "activity",
                        "sub_task_id": st_id,
                        "activity": f"ERROR: {error_detail[:300]}",
                    }),
                )
                raise

        # 7. Hard cutoff — max_iterations reached
        await self._post_system_message(
            f"**Sub-task failed after {max_iterations} iterations:** {sub_task['title']}"
        )
        await self._append_progress_log(
            sub_task, max_iterations, "failed_max_iterations", iteration_log,
        )
        await self._transition_subtask(
            st_id, "failed",
            error_message=f"Max iterations ({max_iterations}) reached without passing quality checks",
        )

    async def _build_iteration_context(
        self,
        sub_task: dict,
        iteration: int,
        iteration_log: list[dict],
        workspace_path: str | None,
        work_rules: dict,
        unstuck_advice: str | None = None,
        agent_config: dict | None = None,
    ) -> dict:
        """Build completely fresh context for one RALPH iteration."""
        todo = await self._load_todo()
        previous_results = await self._get_completed_results()
        intake = safe_json(todo.get("intake_data"))

        # Determine if this is a dependency repo sub-task
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        is_dep_workspace = bool(target_repo and target_repo.get("repo_url"))

        # Workspace context
        workspace_context = ""
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=4)

            if is_dep_workspace:
                dep_name = target_repo.get("name", "dependency")
                workspace_context = (
                    f"\n\nYou are working inside the DEPENDENCY repository: {dep_name}\n"
                    f"Your changes will create a PR on this dependency's repo.\n"
                    f"Repository file structure:\n{file_tree}\n"
                )
                main_repo_dir = os.path.join(workspace_path, "main_repo")
                if os.path.isdir(main_repo_dir):
                    workspace_context += (
                        "\nThe main project repo is available for reference (read-only) at "
                        "../main_repo/ relative to repo root.\n"
                    )
            else:
                workspace_context = (
                    f"\n\nYou are working inside the project repository root directory.\n"
                    f"Project file structure:\n{file_tree}\n"
                )

            # List available dependency repos
            task_deps_dir = os.path.join(workspace_path, "deps")
            if os.path.isdir(task_deps_dir):
                dep_entries = [d for d in sorted(os.listdir(task_deps_dir))
                               if os.path.isdir(os.path.join(task_deps_dir, d))]
                if dep_entries:
                    workspace_context += (
                        "\nDependency repos available (read-only) via ../deps/{name}/path:\n"
                    )
                    for d in dep_entries:
                        workspace_context += f"  - ../deps/{d}/\n"

            # Git diff of current changes
            try:
                repo_dir = os.path.join(workspace_path, "repo")
                if os.path.isdir(repo_dir):
                    proc = await asyncio.create_subprocess_exec(
                        "git", "diff", "--stat",
                        cwd=repo_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    diff_stat = stdout.decode(errors="replace").strip()
                    if diff_stat:
                        workspace_context += f"\nCurrent changes (git diff --stat):\n{diff_stat}\n"
            except Exception:
                pass

        # Work rules block
        rules_block = self._format_rules_for_prompt(work_rules)

        # System prompt — use custom agent config if available
        role = sub_task["agent_role"]
        if agent_config and agent_config.get("system_prompt"):
            system = agent_config["system_prompt"]
        else:
            system = get_default_system_prompt(role)
        system += workspace_context
        system += rules_block

        # For debugger agents, inject debug context
        if role == "debugger":
            debug_block = await self._build_debug_context_block(todo)
            if debug_block:
                system += debug_block

        # Inject tool descriptions for models that need explicit instructions
        if workspace_path:
            system += build_tools_prompt_block(role)

        from agents.orchestrator.output_validator import build_structured_output_instruction
        system += build_structured_output_instruction(role)

        # Completion instruction
        system += (
            "\n\n## IMPORTANT: Signaling Completion\n"
            "When you have finished ALL your work (code written, tests passing, etc.), "
            "you MUST call the `task_complete` tool with a summary of what you accomplished. "
            "This stops the iteration loop. Do NOT keep working after you are done."
        )

        # ── Repo Map (tree-sitter + PageRank) ──
        if workspace_path:
            try:
                from agents.indexing.indexer import RepoIndexer
                from agents.indexing.repo_map import render_repo_map
                from agents.utils.token_counter import count_tokens

                repo_dir_for_map = os.path.join(workspace_path, "repo")
                if os.path.isdir(repo_dir_for_map):
                    # Use shared per-project index directory
                    shared_index_dir = os.path.join(workspace_path, "..", ".agent_index")
                    indexer = RepoIndexer()
                    graph = indexer.index(repo_dir_for_map, cache_dir=shared_index_dir)
                    if graph.symbol_count > 0:
                        repo_map_budget = settings.repo_map_token_budget
                        repo_map = render_repo_map(
                            graph,
                            token_budget=repo_map_budget,
                            count_tokens_fn=lambda t: count_tokens(t, "default"),
                        )
                        system += f"\n\n## Repository Symbol Map\n{repo_map}\n"
                        logger.info(
                            "[%s] Injected repo map: %d symbols, %d files",
                            self.todo_id[:8], graph.symbol_count, graph.file_count,
                        )
            except ImportError:
                logger.debug("[%s] tree-sitter indexing not available", self.todo_id[:8])
            except Exception:
                logger.debug("[%s] Repo map generation failed", self.todo_id[:8], exc_info=True)

        # ── Project Memories ──
        try:
            memories_rows = await self.db.fetch(
                """
                SELECT content, category, confidence FROM project_memories
                WHERE project_id = $1
                ORDER BY confidence DESC
                LIMIT 10
                """,
                str(todo["project_id"]),
            )
            if memories_rows:
                memories_block = "\n\n## Project Memories (learnings from past tasks)\n"
                for m in memories_rows:
                    memories_block += f"- [{m['category']}] {m['content']}\n"
                system += memories_block
                logger.info("[%s] Injected %d project memories", self.todo_id[:8], len(memories_rows))
        except Exception:
            logger.debug("[%s] Failed to load project memories", self.todo_id[:8], exc_info=True)

        # ── Iteration Learnings (with LLM compaction for old entries) ──
        if iteration_log:
            try:
                from agents.utils.context_compaction import compact_iteration_log, format_compacted_entry
                provider = await self._get_provider()
                compacted_log = await compact_iteration_log(
                    iteration_log, provider,
                    keep_recent=settings.context_compaction_keep_recent,
                )
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in compacted_log:
                    if "_compacted" in entry:
                        learnings_block += format_compacted_entry(entry) + "\n"
                    else:
                        status = "PASSED" if entry.get("outcome") == "passed" else f"FAILED ({entry.get('outcome', '?')})"
                        learnings_block += f"- Iteration {entry.get('iteration', '?')}: {status}"
                        if entry.get("learnings"):
                            learnings_block += " — " + "; ".join(entry["learnings"])
                        learnings_block += "\n"
                        if entry.get("error_output"):
                            err = entry["error_output"][:500]
                            learnings_block += f"  Error: {err}\n"
                system += learnings_block
            except ImportError:
                # Fallback: original behavior without compaction
                recent = iteration_log[-5:]
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in recent:
                    status = "PASSED" if entry["outcome"] == "passed" else f"FAILED ({entry['outcome']})"
                    learnings_block += f"- Iteration {entry['iteration']}: {status}"
                    if entry.get("learnings"):
                        learnings_block += " — " + "; ".join(entry["learnings"])
                    learnings_block += "\n"
                    if entry.get("error_output"):
                        err = entry["error_output"][:500]
                        learnings_block += f"  Error: {err}\n"
                system += learnings_block
            except Exception:
                logger.debug("[%s] Context compaction failed, using raw log", self.todo_id[:8], exc_info=True)
                recent = iteration_log[-5:]
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in recent:
                    status = "PASSED" if entry.get("outcome") == "passed" else f"FAILED ({entry.get('outcome', '?')})"
                    learnings_block += f"- Iteration {entry.get('iteration', '?')}: {status}"
                    if entry.get("learnings"):
                        learnings_block += " — " + "; ".join(entry["learnings"])
                    learnings_block += "\n"
                    if entry.get("error_output"):
                        err = entry["error_output"][:500]
                        learnings_block += f"  Error: {err}\n"
                system += learnings_block

        # Unstuck advice injection
        if unstuck_advice:
            system += (
                f"\n\n## STUCK DETECTED\n{unstuck_advice}\n"
                "Try a fundamentally different approach.\n"
            )

        # Previous results context (truncate to save tokens)
        prev_context = ""
        if previous_results:
            prev_items = []
            for r in previous_results:
                out = r.get("output_result", {})
                summary = out.get("approach", "") or out.get("summary", "") if isinstance(out, dict) else ""
                prev_items.append(
                    f"- [{r.get('agent_role', '?')}] {r.get('title', '?')}: {summary[:300]}"
                )
            prev_context = "\n\nCompleted sub-tasks:\n" + "\n".join(prev_items)

        user_content = (
            f"Task: {todo['title']}\n"
            f"Sub-task: {sub_task['title']}\n"
            f"Description: {sub_task['description'] or 'N/A'}\n"
            f"Requirements: {json.dumps(intake, default=str)}\n"
            f"Iteration: {iteration}\n"
            f"{prev_context}"
        )

        return {"system": system, "user": user_content}

    async def _run_quality_checks(
        self, workspace_path: str, work_rules: dict, agent_role: str,
    ) -> dict:
        """Run quality check commands in the workspace.

        Returns {"passed": bool, "reason": str, "error_output": str | None, "learnings": list}.
        """
        if agent_role not in ("coder", "tester"):
            return {"passed": True, "reason": "not applicable", "learnings": []}

        quality_rules = work_rules.get("quality", [])
        if not quality_rules:
            return {"passed": True, "reason": "no quality rules", "learnings": []}

        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        all_passed = True
        combined_output = []
        learnings = []

        for cmd in quality_rules:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir)
                if exit_code != 0:
                    all_passed = False
                    combined_output.append(f"[FAIL] {cmd}:\n{output[:500]}")
                    learnings.append(f"Quality check failed: {cmd}")
                else:
                    learnings.append(f"Quality check passed: {cmd}")
            except Exception as e:
                all_passed = False
                combined_output.append(f"[ERROR] {cmd}: {str(e)[:200]}")

        if all_passed:
            return {"passed": True, "reason": "all checks passed", "learnings": learnings}

        return {
            "passed": False,
            "reason": "failed_quality",
            "error_output": "\n".join(combined_output),
            "learnings": learnings,
        }

    async def _check_if_stuck(
        self, iteration_log: list[dict], provider: AIProvider,
    ) -> dict:
        """Use an isolated LLM call to analyze if the agent is stuck."""
        last_5 = iteration_log[-5:]
        condensed = [
            {
                "iteration": e["iteration"],
                "outcome": e["outcome"],
                "learnings": e.get("learnings", []),
                "error_output": (e.get("error_output") or "")[:300],
            }
            for e in last_5
        ]

        messages = [
            LLMMessage(role="system", content=STUCK_DETECTOR_PROMPT),
            LLMMessage(role="user", content=json.dumps(condensed, indent=2)),
        ]

        try:
            response = await provider.send_message(messages, temperature=0.0)
            result = parse_llm_json(response.content)
            if result is None:
                return {"stuck": False, "advice": None, "pattern": None}
            return {
                "stuck": result.get("stuck", False),
                "advice": result.get("advice"),
                "pattern": result.get("pattern"),
            }
        except Exception:
            logger.warning("Stuck detection LLM call failed", exc_info=True)
            return {"stuck": False, "advice": None, "pattern": None}

    async def _append_progress_log(
        self,
        sub_task: dict,
        iterations_used: int,
        outcome: str,
        iteration_log: list[dict],
    ) -> None:
        """Append a sub-task completion record to todo_items.progress_log."""
        key_learnings = []
        for entry in iteration_log:
            for learning in entry.get("learnings", []):
                if learning not in key_learnings:
                    key_learnings.append(learning)
        key_learnings = key_learnings[-10:]  # Keep last 10 unique

        record = {
            "sub_task_id": str(sub_task["id"]),
            "sub_task_title": sub_task["title"],
            "iterations_used": iterations_used,
            "outcome": outcome,
            "key_learnings": key_learnings,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        # Atomic append to progress_log
        await self.db.execute(
            """
            UPDATE todo_items
            SET progress_log = COALESCE(progress_log, '[]'::jsonb) || $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            self.todo_id,
            [record],
        )

    # ---- REVIEW-MERGE LOOP ----

    async def _handle_subtask_completion(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """After a sub-task completes, check if it's part of a review loop
        and create follow-up sub-tasks (reviewer, fix, merge_agent) dynamically."""
        # Reload sub-task to get latest status
        st = await self.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE id = $1", sub_task["id"]
        )
        if not st or st["status"] != "completed":
            return

        # Only process review-loop sub-tasks or chained reviewers.
        # Parallel fix coders (review_loop=False, role=coder) must NOT enter
        # this handler — they would each spawn a duplicate reviewer.
        role = st["agent_role"]
        is_review_loop_task = st.get("review_loop")
        is_chained_reviewer = (role == "reviewer" and st.get("review_chain_id"))
        if not is_review_loop_task and not is_chained_reviewer:
            return

        chain_id = st.get("review_chain_id") or st["id"]

        # Safety: count sub-tasks in this chain
        chain_count = await self.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks WHERE review_chain_id = $1",
            chain_id,
        )
        if chain_count >= MAX_REVIEW_ROUNDS * 2:
            logger.warning(
                "Review chain %s hit max rounds (%d sub-tasks), stopping",
                chain_id, chain_count,
            )
            await self._post_system_message(
                f"**Review chain capped at {MAX_REVIEW_ROUNDS} rounds.** "
                "Creating merge sub-task with current state."
            )
            await self._create_merge_subtask(st, chain_id)
            return

        if role == "coder":
            # Coder finished → create reviewer to review the workspace changes.
            # Commit/push/PR happens later, after reviewer approval.
            await self._create_reviewer_subtask(st, chain_id, workspace_path)

        elif role == "reviewer":
            # Parse verdict — prefer structured output field, fall back to heuristic
            verdict = st.get("review_verdict")
            if not verdict:
                output = st.get("output_result") or {}
                if isinstance(output, dict):
                    verdict = output.get("verdict")  # Direct from validated schema
                if not verdict:
                    # Legacy fallback
                    verdict = self._extract_review_verdict(
                        output.get("content", "") if isinstance(output, dict) else str(output)
                    )
                await self.db.execute(
                    "UPDATE sub_tasks SET review_verdict = $2 WHERE id = $1",
                    st["id"], verdict,
                )

            if verdict == "approved":
                # Create a pr_creator sub-task to handle commit+push+PR.
                # It will in turn create a merge_agent sub-task on success.
                await self._post_system_message(
                    "**Review approved.** Creating PR sub-task..."
                )
                await self._create_pr_creator_subtask(st, chain_id)
            else:
                # needs_changes → create a new coder fix sub-task with detailed issue info
                output = st.get("output_result") or {}
                if isinstance(output, dict):
                    reviewer_feedback = output.get("content", "")
                    structured_issues = output.get("issues", [])
                else:
                    reviewer_feedback = str(output)
                    structured_issues = []
                await self._create_fix_subtasks(
                    st, chain_id, reviewer_feedback, structured_issues,
                )

    async def _create_reviewer_subtask(
        self, coder_st: dict, chain_id, workspace_path: str | None = None,
    ) -> None:
        """Create a reviewer sub-task that depends on the completed coder sub-task.

        Captures the git diff from the workspace and includes it in the
        reviewer's description so it has full context of what changed.
        """
        target_repo_json = coder_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        # Build rich description with coder output + git diff
        desc_parts = [
            f"Review the changes from sub-task '{coder_st['title']}'.",
            "Check for bugs, security issues, code quality, and adherence to requirements.",
        ]

        # Include coder's output info (files changed, approach)
        coder_output = coder_st.get("output_result") or {}
        if isinstance(coder_output, dict):
            approach = coder_output.get("approach", "")
            if approach:
                desc_parts.append(f"\n## Implementation Approach\n{approach}")
            files_changed = coder_output.get("files_changed", [])
            if files_changed:
                desc_parts.append("\n## Files Changed\n" + "\n".join(f"- {f}" for f in files_changed))

        # Capture git diff from workspace
        if workspace_path:
            try:
                repo_dir = os.path.join(workspace_path, "repo")
                if not os.path.isdir(repo_dir):
                    repo_dir = workspace_path

                async def _git(args):
                    proc = await asyncio.create_subprocess_exec(
                        "git", *args, cwd=repo_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out, _ = await proc.communicate()
                    return out.decode(errors="replace").strip()

                # Get diff stat and full diff (uncommitted + staged)
                diff_stat = await _git(["diff", "--stat", "HEAD"])
                diff_full = await _git(["diff", "HEAD"])

                # Fallback: diff against last commit if no uncommitted changes
                if not diff_full:
                    diff_stat = await _git(["diff", "--stat", "HEAD~1", "HEAD"])
                    diff_full = await _git(["diff", "HEAD~1", "HEAD"])

                if diff_stat:
                    desc_parts.append(f"\n## Git Diff Summary\n```\n{diff_stat}\n```")
                if diff_full:
                    # Truncate large diffs
                    if len(diff_full) > 10_000:
                        diff_full = diff_full[:10_000] + "\n... (truncated, use git diff to see full changes)"
                    desc_parts.append(f"\n## Full Diff\n```diff\n{diff_full}\n```")
            except Exception:
                logger.warning("[%s] Failed to capture git diff for reviewer", self.todo_id, exc_info=True)

        desc_parts.append(
            "\n## Instructions\n"
            "Review the diff above carefully. For each issue found, specify the exact "
            "file path and line number.\n\n"
            "You MUST output a JSON verdict at the end of your response:\n"
            '{"verdict": "approved"} or {"verdict": "needs_changes", "issues": ['
            '{"severity": "major", "file": "path/to/file.py", "line": 42, '
            '"description": "what is wrong", "suggestion": "how to fix it"}]}'
        )

        description = "\n".join(desc_parts)

        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'reviewer', $4, $5, $6, $7)
            RETURNING id
            """,
            self.todo_id,
            f"Review: {coder_st['title']}",
            description,
            (coder_st.get("execution_order") or 0) + 1,
            [str(coder_st["id"])],
            chain_id,
            target_repo_json,
        )
        logger.info("Created reviewer sub-task %s for chain %s", row["id"], chain_id)
        await self._post_system_message(
            f"**Review loop:** Created reviewer sub-task for '{coder_st['title']}'"
        )

    async def _create_fix_subtasks(
        self, reviewer_st: dict, chain_id, feedback: str,
        structured_issues: list | None = None,
    ) -> None:
        """Create fix sub-tasks from reviewer feedback.

        If issues span multiple files, creates parallel per-file fix
        sub-tasks plus a pre-created reviewer that depends on all of them.
        Otherwise falls back to a single fix sub-task (legacy path).
        """
        # Group issues by file
        file_groups: dict[str, list[dict]] = {}
        if structured_issues:
            for issue in structured_issues:
                if isinstance(issue, dict):
                    key = issue.get("file") or "_general"
                    file_groups.setdefault(key, []).append(issue)

        # Single-file or no structured issues → legacy single fix sub-task
        if len(file_groups) <= 1:
            await self._create_single_fix_subtask(
                reviewer_st, chain_id, feedback, structured_issues,
            )
            return

        # Multi-file → parallel fix sub-tasks + pre-created reviewer
        target_repo_json = reviewer_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        base_order = (reviewer_st.get("execution_order") or 0) + 1
        base_title = reviewer_st["title"].removeprefix("Review: ")
        fix_task_ids: list[str] = []

        for file_key, file_issues in file_groups.items():
            description = self._build_fix_description_for_file(
                file_key, file_issues, reviewer_st,
            )
            display_file = file_key if file_key != "_general" else "general"
            row = await self.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on,
                    review_loop, review_chain_id, target_repo
                )
                VALUES ($1, $2, $3, 'coder', $4, $5, FALSE, $6, $7)
                RETURNING id
                """,
                self.todo_id,
                f"Fix ({display_file}): {base_title}",
                description,
                base_order,
                [str(reviewer_st["id"])],
                chain_id,
                target_repo_json,
            )
            fix_task_ids.append(str(row["id"]))
            logger.info(
                "Created parallel fix sub-task %s for file %s in chain %s",
                row["id"], file_key, chain_id,
            )

        # Pre-create a reviewer that depends on all parallel fix tasks
        reviewer_row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on,
                review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'reviewer', $4, $5, FALSE, $6, $7)
            RETURNING id
            """,
            self.todo_id,
            f"Review: {base_title}",
            "Review the workspace after parallel fixes. Check that all issues are resolved.",
            base_order + 1,
            fix_task_ids,
            chain_id,
            target_repo_json,
        )
        logger.info(
            "Created pre-reviewer sub-task %s (depends on %d fixes) for chain %s",
            reviewer_row["id"], len(fix_task_ids), chain_id,
        )
        await self._post_system_message(
            f"**Review loop:** Reviewer requested changes across {len(file_groups)} files. "
            f"Created {len(fix_task_ids)} parallel fix sub-tasks + reviewer."
        )

    async def _create_single_fix_subtask(
        self, reviewer_st: dict, chain_id, feedback: str,
        structured_issues: list | None = None,
    ) -> None:
        """Create a single coder fix sub-task (legacy path).

        Used when all issues are in one file or have no file info.
        """
        target_repo_json = reviewer_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        # Build detailed fix description from structured issues
        desc_parts = ["Address the reviewer's feedback and fix the following issues:\n"]

        if structured_issues:
            for i, issue in enumerate(structured_issues, 1):
                if isinstance(issue, dict):
                    severity = issue.get("severity", "major").upper()
                    file_path = issue.get("file", "")
                    line = issue.get("line")
                    issue_desc = issue.get("description", "")
                    suggestion = issue.get("suggestion", "")

                    location = ""
                    if file_path:
                        location = f" in `{file_path}`"
                        if line:
                            location += f" (line {line})"

                    desc_parts.append(f"### Issue {i} [{severity}]{location}")
                    if issue_desc:
                        desc_parts.append(f"**Problem:** {issue_desc}")
                    if suggestion:
                        desc_parts.append(f"**Fix:** {suggestion}")
                    desc_parts.append("")
                else:
                    desc_parts.append(f"- {str(issue)}")

        # Also include reviewer summary if available
        reviewer_output = reviewer_st.get("output_result") or {}
        if isinstance(reviewer_output, dict):
            summary = reviewer_output.get("summary", "")
            if summary:
                desc_parts.append(f"\n## Reviewer Summary\n{summary}")

        # Include raw feedback as fallback context (truncated)
        if feedback and not structured_issues:
            desc_parts.append(f"\n## Reviewer Feedback\n{feedback[:3000]}")

        description = "\n".join(desc_parts)

        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'coder', $4, $5, TRUE, $6, $7)
            RETURNING id
            """,
            self.todo_id,
            f"Fix: {reviewer_st['title'].removeprefix('Review: ')}",
            description,
            (reviewer_st.get("execution_order") or 0) + 1,
            [str(reviewer_st["id"])],
            chain_id,
            target_repo_json,
        )
        logger.info("Created fix sub-task %s for chain %s", row["id"], chain_id)
        await self._post_system_message(
            "**Review loop:** Reviewer requested changes. Created fix sub-task."
        )

    @staticmethod
    def _build_fix_description_for_file(
        file_key: str, issues: list[dict], reviewer_st: dict,
    ) -> str:
        """Build a focused fix description for issues in a single file."""
        if file_key == "_general":
            parts = ["Fix the following general issues:\n"]
        else:
            parts = [f"Fix the following issues in `{file_key}`:\n"]

        for i, issue in enumerate(issues, 1):
            severity = issue.get("severity", "major").upper()
            line = issue.get("line")
            issue_desc = issue.get("description", "")
            suggestion = issue.get("suggestion", "")

            line_ref = f" (line {line})" if line else ""
            parts.append(f"### Issue {i} [{severity}]{line_ref}")
            if issue_desc:
                parts.append(f"**Problem:** {issue_desc}")
            if suggestion:
                parts.append(f"**Fix:** {suggestion}")
            parts.append("")

        # Include reviewer summary for context
        reviewer_output = reviewer_st.get("output_result") or {}
        if isinstance(reviewer_output, dict):
            summary = reviewer_output.get("summary", "")
            if summary:
                parts.append(f"\n## Reviewer Summary\n{summary}")

        return "\n".join(parts)

    async def _create_merge_subtask(self, approved_st: dict, chain_id) -> None:
        """Create a merge_agent sub-task after reviewer approval.

        The merge task depends on ALL subtasks in the same review chain
        (coder + reviewer + any fix iterations) so it only runs after
        everything is complete.
        """
        # Gather all subtask IDs in this review chain as dependencies
        chain_tasks = await self.db.fetch(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND "
            "(review_chain_id = $2 OR id = $2)",
            self.todo_id,
            chain_id,
        )
        depends_on = [str(t["id"]) for t in chain_tasks]
        # Ensure the approved reviewer subtask is included
        approved_id = str(approved_st["id"])
        if approved_id not in depends_on:
            depends_on.append(approved_id)

        target_repo_json = approved_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)
        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'merge_agent', $4, $5, $6, $7)
            RETURNING id
            """,
            self.todo_id,
            "Merge PR for review chain",
            "Merge the approved PR. Check CI status, merge, and run post-merge builds if configured.",
            (approved_st.get("execution_order") or 0) + 1,
            depends_on,
            chain_id,
            target_repo_json,
        )
        logger.info("Created merge_agent sub-task %s for chain %s (depends on %d tasks)", row["id"], chain_id, len(depends_on))
        await self._post_system_message(
            "**Review loop:** Reviewer approved. Created merge sub-task."
        )

    async def _create_pr_creator_subtask(self, approved_st: dict, chain_id=None) -> None:
        """Create a pr_creator sub-task after reviewer approval (review chain flow).

        The pr_creator depends on ALL subtasks in the review chain so it only
        runs after everything is complete. It will create a merge_agent on success.
        """
        depends_on = []
        if chain_id:
            chain_tasks = await self.db.fetch(
                "SELECT id FROM sub_tasks WHERE todo_id = $1 AND "
                "(review_chain_id = $2 OR id = $2)",
                self.todo_id,
                chain_id,
            )
            depends_on = [str(t["id"]) for t in chain_tasks]

        # Ensure the approved subtask is included
        approved_id = str(approved_st["id"])
        if approved_id not in depends_on:
            depends_on.append(approved_id)

        target_repo_json = approved_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6, $7)
            RETURNING id
            """,
            self.todo_id,
            "Create Pull Request",
            "Commit all workspace changes, push to a feature branch, and create a pull request.",
            (approved_st.get("execution_order") or 0) + 1,
            depends_on,
            chain_id,
            target_repo_json,
        )
        logger.info("Created pr_creator sub-task %s for chain %s", row["id"], chain_id)
        await self._post_system_message(
            "**Review loop:** Reviewer approved. Created PR sub-task."
        )

    # ---- CODING GUARDRAILS ----

    async def _ensure_coding_guardrails(self, workspace_path: str | None) -> bool:
        """Check for missing tester/reviewer subtasks on coding tasks.

        When all subtasks are done and there are completed coder subtasks
        that are NOT in a review chain, this method auto-creates tester
        and reviewer subtasks to ensure code quality before completion.

        Returns True if new guardrail subtasks were created (caller should
        re-enter execution), False if no action needed.
        """
        # Check if any coder subtask completed outside a review chain
        coder_subtasks = await self.db.fetch(
            "SELECT id, title, review_loop, review_chain_id FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
            self.todo_id,
        )
        if not coder_subtasks:
            return False  # No coder work → no guardrails needed

        # Filter to coder subtasks NOT in a review chain (those already have their own reviewer)
        unreviewed_coders = [
            st for st in coder_subtasks
            if not st["review_loop"] and not st["review_chain_id"]
        ]
        if not unreviewed_coders:
            return False  # All coder work is in review chains — already covered

        # Check what roles already exist outside of review chains
        existing_roles = await self.db.fetch(
            "SELECT DISTINCT agent_role FROM sub_tasks "
            "WHERE todo_id = $1 AND (review_chain_id IS NULL)",
            self.todo_id,
        )
        existing = {r["agent_role"] for r in existing_roles}

        created: list[tuple[str, str]] = []
        coder_ids = [str(st["id"]) for st in unreviewed_coders]
        coder_titles = [st["title"] for st in unreviewed_coders]

        # 1. Tester guardrail
        if "tester" not in existing:
            tester_desc = (
                "Write and run tests to validate the code changes made by the coder subtask(s):\n"
                + "\n".join(f"- {t}" for t in coder_titles)
                + "\n\nFocus on: unit tests for new/changed functions, edge cases, "
                "and regression tests. Run the test suite and report results."
            )
            tester_id = await self._create_guardrail_subtask(
                title="Test implemented changes",
                description=tester_desc,
                role="tester",
                depends_on=coder_ids,
            )
            created.append(("tester", tester_id))

        # 2. Reviewer guardrail
        if "reviewer" not in existing:
            deps = list(coder_ids)
            if created:  # tester was just created, reviewer depends on it too
                deps.append(created[-1][1])
            reviewer_desc = (
                "Review all code changes for quality, security, correctness, and adherence "
                "to requirements. Check the coder subtask(s):\n"
                + "\n".join(f"- {t}" for t in coder_titles)
                + "\n\nYou MUST output a JSON verdict at the end of your response:\n"
                '{"verdict": "approved"} or {"verdict": "needs_changes", "issues": ["issue1", ...]}'
            )
            reviewer_id = await self._create_guardrail_subtask(
                title="Review code changes",
                description=reviewer_desc,
                role="reviewer",
                depends_on=deps,
            )
            created.append(("reviewer", reviewer_id))

        if created:
            roles = ", ".join(r for r, _ in created)
            logger.info(
                "[%s] Coding guardrails: auto-created %s subtask(s) for unreviewed coder work",
                self.todo_id, roles,
            )
            await self._post_system_message(
                f"**Guardrail:** Auto-created {roles} subtask(s) to ensure code quality."
            )
            return True

        return False

    async def _create_guardrail_subtask(
        self,
        title: str,
        description: str,
        role: str,
        depends_on: list[str],
    ) -> str:
        """Create a guardrail subtask and return its ID as string."""
        # Compute execution_order: max of dependencies + 1
        max_order = 0
        if depends_on:
            rows = await self.db.fetch(
                "SELECT execution_order FROM sub_tasks WHERE id = ANY($1)",
                depends_on,
            )
            max_order = max((r["execution_order"] or 0) for r in rows) if rows else 0

        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            self.todo_id,
            title,
            description,
            role,
            max_order + 1,
            depends_on,
        )
        st_id = str(row["id"])
        logger.info(
            "[%s] Created guardrail subtask %s: role=%s title=%s depends_on=%s",
            self.todo_id, st_id, role, title, depends_on,
        )
        return st_id

    async def _handle_create_subtask_tool(
        self, parent_subtask: dict, args: dict, workspace_path: str | None,
    ) -> str:
        """Handle the create_subtask builtin tool called by a coder agent.

        Creates a new subtask under the same todo. The new subtask will be
        picked up by the re-scan loop after the current batch completes.
        """
        title = args.get("title", "").strip()
        description = args.get("description", "").strip()
        agent_role = args.get("agent_role", "coder").strip()

        if not title:
            return json.dumps({"error": "title is required"})
        if not description:
            return json.dumps({"error": "description is required"})
        if agent_role not in ("coder", "tester", "reviewer", "debugger"):
            return json.dumps({"error": f"Invalid agent_role: {agent_role}. Use coder, tester, reviewer, or debugger."})

        # New subtask gets execution_order after the parent
        parent_order = parent_subtask.get("execution_order") or 0

        try:
            row = await self.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                self.todo_id,
                title,
                description,
                agent_role,
                parent_order,
                [],  # no dependencies — available immediately for parallel execution
            )
            st_id = str(row["id"])
            logger.info(
                "[%s] Coder created child subtask %s: role=%s title=%s (parent=%s)",
                self.todo_id, st_id, agent_role, title, parent_subtask["id"],
            )
            await self._post_system_message(
                f"**Subtask created by {parent_subtask['agent_role']}:** "
                f"[{agent_role}] {title}"
            )
            return json.dumps({
                "status": "created",
                "subtask_id": st_id,
                "title": title,
                "agent_role": agent_role,
                "message": f"Subtask created. It will be executed after current batch completes.",
            })
        except Exception as e:
            logger.error("[%s] Failed to create child subtask: %s", self.todo_id, e)
            return json.dumps({"error": f"Failed to create subtask: {str(e)}"})

    async def _finalize_subtask_workspace(
        self, sub_task: dict, workspace_path: str | None,
    ) -> dict | None:
        """Deterministic commit → push → PR for a sub-task's workspace changes.

        Called after reviewer approval (not after coder completion) so that
        only reviewed code gets a PR.
        """
        if not workspace_path:
            logger.warning("[%s] Cannot finalize: no workspace_path", self.todo_id)
            await self._post_system_message("**PR creation skipped:** no workspace path available.")
            return None

        try:
            todo = await self._load_todo()
            short_id = str(self.todo_id)[:8]

            # Determine if this is a dependency repo sub-task
            target_repo = sub_task.get("target_repo")
            if isinstance(target_repo, str):
                target_repo = json.loads(target_repo)

            if target_repo and target_repo.get("repo_url"):
                dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
                branch_name = f"task/{short_id}-{dep_name}"
                base_branch = target_repo.get("default_branch") or "main"
            else:
                project = await self.db.fetchrow(
                    "SELECT repo_url, default_branch FROM projects WHERE id = $1",
                    todo["project_id"],
                )
                if not project or not project.get("repo_url"):
                    logger.warning("[%s] Cannot finalize: no repo_url on project", self.todo_id)
                    await self._post_system_message("**PR creation skipped:** no repository URL configured on the project.")
                    return None
                branch_name = f"task/{short_id}"
                base_branch = project.get("default_branch") or "main"

            logger.info("[%s] Finalizing workspace: branch=%s base=%s dep=%s path=%s",
                        self.todo_id, branch_name, base_branch,
                        bool(target_repo), workspace_path)

            # Step 1: commit and push
            pushed = await self.workspace_mgr.commit_and_push(
                workspace_path,
                message=f"[agents] {sub_task['title']}",
                branch=branch_name,
            )
            if not pushed:
                logger.error("[%s] commit_and_push failed for branch %s", self.todo_id, branch_name)
                await self._post_system_message(f"**PR creation failed:** could not push to branch `{branch_name}`.")
                return None

            logger.info("[%s] Pushed to branch %s", self.todo_id, branch_name)

            # Step 2: check if PR already exists for this branch
            existing_pr = await self.db.fetchrow(
                "SELECT id, pr_number, pr_url FROM deliverables "
                "WHERE todo_id = $1 AND type = 'pull_request' AND branch_name = $2",
                self.todo_id,
                branch_name,
            )
            if existing_pr:
                logger.info("[%s] PR already exists: #%s", self.todo_id, existing_pr["pr_number"])
                return {"number": existing_pr["pr_number"], "url": existing_pr.get("pr_url")}

            # Step 3: create PR
            if target_repo and target_repo.get("repo_url"):
                pr_info = await self.workspace_mgr.create_pr_for_repo(
                    repo_url=target_repo["repo_url"],
                    git_provider_id=target_repo.get("git_provider_id"),
                    head_branch=branch_name,
                    base_branch=base_branch,
                    title=todo["title"],
                    body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
                )
            else:
                pr_info = await self.workspace_mgr.create_pr(
                    str(todo["project_id"]),
                    head_branch=branch_name,
                    base_branch=base_branch,
                    title=todo["title"],
                    body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
                )

            # Step 4: record deliverable
            if pr_info:
                logger.info("[%s] PR created: %s", self.todo_id, pr_info.get("url"))
                await self.db.execute(
                    """
                    INSERT INTO deliverables (
                        todo_id, sub_task_id, type, title,
                        pr_url, pr_number, branch_name, status
                    )
                    VALUES ($1, $2, 'pull_request', $3, $4, $5, $6, 'pending')
                    """,
                    self.todo_id,
                    sub_task["id"],
                    f"PR: {todo['title']}",
                    pr_info.get("url"),
                    pr_info.get("number"),
                    branch_name,
                )
            else:
                logger.warning("[%s] create_pr returned empty result", self.todo_id)
                await self._post_system_message("**PR creation failed:** git provider returned no PR data.")

            return pr_info
        except Exception:
            logger.error("[%s] Failed to finalize subtask workspace", self.todo_id, exc_info=True)
            await self._post_system_message("**PR creation failed:** unexpected error. Check server logs.")
            return None

    def _extract_review_verdict(self, content: str) -> str:
        """Parse reviewer verdict from output content."""
        # Try structured JSON extraction
        data = parse_llm_json(content)
        if data is not None:
            verdict = data.get("verdict", "").lower()
            if verdict in ("approved", "needs_changes"):
                return verdict

        # Heuristic fallback
        lower = content.lower()
        if "approved" in lower and "needs_changes" not in lower:
            return "approved"
        if "needs_changes" in lower or "needs changes" in lower or "request changes" in lower:
            return "needs_changes"

        # Default to needs_changes if ambiguous — conservative safety
        return "needs_changes"

    # ---- PR CREATOR ----

    async def _execute_pr_creator_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural PR creator: commit, push, create PR, then spawn merge_agent.

        No LLM needed — this is a deterministic git operation.
        """
        st_id = str(sub_task["id"])
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        await self.db.execute(
            "UPDATE todo_items SET sub_state = 'creating_pr', updated_at = NOW() WHERE id = $1",
            self.todo_id,
        )

        try:
            if not workspace_path:
                raise ValueError("No workspace path available for PR creation")

            # Use the sub-task itself as context for _finalize_subtask_workspace
            # (it will determine branch name and PR details from the todo)
            await self._report_progress(st_id, 10, "Preparing to commit and push")

            # Find the latest coder subtask for commit context
            coder_st = await self.db.fetchrow(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
                "AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
                self.todo_id,
            )
            commit_st = coder_st or sub_task

            await self._report_progress(st_id, 30, "Committing and pushing changes")
            pr_info = await self._finalize_subtask_workspace(commit_st, workspace_path)

            if not pr_info:
                raise ValueError("PR creation failed — no PR data returned")

            pr_url = pr_info.get("url", "N/A")
            await self._report_progress(st_id, 80, f"PR created: {pr_url}")
            await self._post_system_message(
                f"**PR created:** {pr_url}. Creating merge sub-task."
            )

            # Create a merge_agent subtask that depends on this pr_creator
            all_coder_ids = [str(sub_task["id"])]
            if coder_st:
                # Also depend on the coder subtask
                all_coder_ids.append(str(coder_st["id"]))

            target_repo_json = sub_task.get("target_repo")
            if isinstance(target_repo_json, str):
                target_repo_json = json.loads(target_repo_json)

            max_order = await self.db.fetchval(
                "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
                self.todo_id,
            )

            await self.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, target_repo
                )
                VALUES ($1, $2, $3, 'merge_agent', $4, $5, $6)
                RETURNING id
                """,
                self.todo_id,
                "Merge PR",
                "Merge the PR. Check CI status, merge, and run post-merge builds if configured.",
                max_order + 1,
                all_coder_ids,
                target_repo_json,
            )

            await self._report_progress(st_id, 100, "PR created successfully")
            await self._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"PR: {pr_url}",
            )

        except Exception as e:
            logger.error("[%s] PR creator failed: %s", self.todo_id, e, exc_info=True)
            await self._post_system_message(
                f"**PR creation failed:** {str(e)[:500]}. You can retry this sub-task."
            )
            await self._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    # ---- MERGE AGENT ----

    async def _execute_merge_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural merge agent: check CI, merge PR, run post-merge builds.

        This is mostly procedural (no LLM needed for happy path).
        """
        st_id = str(sub_task["id"])
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        await self.db.execute(
            "UPDATE todo_items SET sub_state = 'merging', updated_at = NOW() WHERE id = $1",
            self.todo_id,
        )

        try:
            todo = await self._load_todo()
            project = await self.db.fetchrow(
                "SELECT * FROM projects WHERE id = $1", todo["project_id"]
            )
            if not project or not project.get("repo_url"):
                raise ValueError("No repo configured for merge")

            # Find PR deliverable for this task
            pr_deliv = await self.db.fetchrow(
                "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "AND pr_number IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                self.todo_id,
            )
            if not pr_deliv:
                await self._post_system_message("**Merge agent:** No PR found to merge. Skipping.")
                await self._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message="No PR to merge",
                )
                return

            # Resolve git provider
            from agents.orchestrator.git_providers.factory import (
                create_git_provider,
                parse_repo_url,
            )
            from agents.infra.crypto import decrypt

            repo_url = project["repo_url"]
            git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None
            token = None
            provider_type = None
            api_base_url = None
            if git_provider_id:
                gp_row = await self.db.fetchrow(
                    "SELECT provider_type, api_base_url, token_enc "
                    "FROM git_provider_configs WHERE id = $1",
                    git_provider_id,
                )
                if gp_row:
                    token = decrypt(gp_row["token_enc"]) if gp_row.get("token_enc") else None
                    provider_type = gp_row["provider_type"]
                    api_base_url = gp_row.get("api_base_url")

            git = create_git_provider(
                provider_type=provider_type,
                api_base_url=api_base_url,
                token=token,
                repo_url=repo_url,
            )
            owner, repo = parse_repo_url(repo_url)
            pr_number = pr_deliv["pr_number"]

            await self._report_progress(st_id, 20, "Checking PR status")

            # 1. Get PR status
            pr_data = await git.get_pull_request(owner, repo, pr_number)
            if pr_data["state"] != "open":
                await self._post_system_message(
                    f"**Merge agent:** PR #{pr_number} is {pr_data['state']}, not open. Skipping merge."
                )
                await self._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message=f"PR already {pr_data['state']}",
                )
                return

            # 2. Check CI status
            await self._report_progress(st_id, 40, "Checking CI status")
            ci_data = await git.get_check_runs(owner, repo, pr_data["head_sha"])

            if ci_data["state"] == "pending":
                await self._post_system_message(
                    f"**Merge agent:** CI still running for PR #{pr_number}. Will retry on next cycle."
                )
                # Reset to pending so it gets picked up again
                await self._transition_subtask(
                    st_id, "pending",
                    progress_message="Waiting for CI",
                )
                return

            if ci_data["state"] == "failure":
                failed_checks = [c["name"] for c in ci_data.get("checks", []) if c.get("conclusion") == "failure"]
                msg = f"CI failed: {', '.join(failed_checks)}" if failed_checks else "CI checks failed"
                await git.post_pr_comment(
                    owner, repo, pr_number,
                    f"**Agent Merge Bot:** Cannot merge — {msg}",
                )
                await self._post_system_message(
                    f"**Merge agent:** CI failed for PR #{pr_number}. {msg}"
                )
                await self._transition_subtask(
                    st_id, "failed",
                    error_message=msg,
                )
                return

            # 3. Check for unmerged dependency PRs
            await self._report_progress(st_id, 50, "Checking dependency PRs")
            dep_subtasks = await self.db.fetch(
                "SELECT d.pr_state, d.target_repo_name, d.pr_number "
                "FROM sub_tasks st JOIN deliverables d ON d.sub_task_id = st.id "
                "WHERE st.todo_id = $1 AND st.target_repo IS NOT NULL "
                "AND d.type = 'pull_request' AND d.pr_state != 'merged'",
                self.todo_id,
            )
            if dep_subtasks:
                dep_names = [d.get("target_repo_name") or f"PR #{d['pr_number']}" for d in dep_subtasks]
                await self._post_system_message(
                    f"**Merge agent:** Waiting for dependency PRs: {', '.join(dep_names)}"
                )
                await self._transition_subtask(
                    st_id, "pending",
                    progress_message="Waiting for dependency PRs",
                )
                return

            # 4. Check if human approval is required before merge
            project_settings = project.get("settings_json") or {}
            if isinstance(project_settings, str):
                project_settings = json.loads(project_settings)

            require_approval = project_settings.get("require_merge_approval", False)
            already_approved = todo.get("sub_state") == "merge_approved"

            if require_approval and not already_approved:
                await self.db.execute(
                    "UPDATE todo_items SET sub_state = 'awaiting_merge_approval', updated_at = NOW() WHERE id = $1",
                    self.todo_id,
                )
                await self._post_system_message(
                    f"**PR #{pr_number} is ready to merge.** CI passed. Awaiting your approval to merge."
                )
                await self._transition_subtask(
                    st_id, "pending",
                    progress_message="Awaiting merge approval",
                )
                await self.redis.publish(
                    f"task:{self.todo_id}:events",
                    json.dumps({
                        "type": "state_change",
                        "state": "in_progress",
                        "sub_state": "awaiting_merge_approval",
                    }),
                )
                return

            # If approval was granted, clear the sub_state
            if already_approved:
                await self.db.execute(
                    "UPDATE todo_items SET sub_state = 'merging', updated_at = NOW() WHERE id = $1",
                    self.todo_id,
                )

            # 5. Merge the PR
            await self._report_progress(st_id, 70, f"Merging PR #{pr_number}")
            merge_method = project_settings.get("merge_method", "squash")

            merge_result = await git.merge_pull_request(
                owner, repo, pr_number, method=merge_method,
            )

            if not merge_result.get("merged"):
                await self._post_system_message(
                    f"**Merge agent:** Failed to merge PR #{pr_number}: {merge_result.get('message', 'unknown error')}"
                )
                await self._transition_subtask(
                    st_id, "failed",
                    error_message=merge_result.get("message", "Merge failed"),
                )
                return

            # 5. Update deliverable
            await self.db.execute(
                "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
                "merge_method = $2, status = 'approved' WHERE id = $1",
                pr_deliv["id"],
                merge_method,
            )

            await self._post_system_message(
                f"**PR #{pr_number} merged** via {merge_method}. SHA: {merge_result.get('sha', 'N/A')}"
            )

            # 6. Post-merge build commands
            build_commands = project_settings.get("build_commands", [])
            if build_commands and workspace_path:
                await self._report_progress(st_id, 85, "Running post-merge builds")
                await self._run_post_merge_builds(todo, build_commands, workspace_path)

            await self._report_progress(st_id, 100, "Merge complete")
            await self._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message="Merged",
            )

        except Exception as e:
            logger.error("Merge agent failed: %s", e, exc_info=True)
            await self._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    async def _run_post_merge_builds(
        self, todo: dict, build_commands: list[str], workspace_path: str,
    ) -> None:
        """Pull latest after merge and run build commands."""
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        # Pull latest after merge
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "origin",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.communicate()

        for cmd in build_commands:
            try:
                exit_code, output = await self.workspace_mgr.run_command(cmd, repo_dir, timeout=120)
                if exit_code != 0:
                    await self._post_system_message(
                        f"**Post-merge build failed:** `{cmd}`\n```\n{output[:500]}\n```"
                    )
                else:
                    logger.info("Post-merge build passed: %s", cmd)
            except Exception as e:
                await self._post_system_message(
                    f"**Post-merge build error:** `{cmd}` — {str(e)[:200]}"
                )

    async def _execute_subtask(self, sub_task: dict, provider: AIProvider, *, workspace_path: str | None = None) -> None:
        """Execute a single sub-task using the appropriate specialist agent."""
        st_id = str(sub_task["id"])
        logger.info("[%s] _execute_subtask START: st=%s role=%s title=%s workspace=%s",
                    self.todo_id, st_id, sub_task["agent_role"], sub_task["title"], workspace_path)
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        # Update parent TODO sub_state
        await self.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            sub_task["agent_role"],
        )

        # Create agent run record
        run = await self.db.fetchrow(
            """
            INSERT INTO agent_runs (
                todo_id, sub_task_id, agent_role, agent_model, provider_type
            )
            VALUES ($1, $2, $3, $4, $5) RETURNING *
            """,
            self.todo_id,
            sub_task["id"],
            sub_task["agent_role"],
            provider.default_model,
            provider.provider_type,
        )

        start_time = time.monotonic()

        # Build context for the agent
        previous_results = await self._get_completed_results()
        todo = await self._load_todo()

        # Resolve custom agent config
        agent_config = await self._resolve_agent_config(
            sub_task["agent_role"], str(todo["creator_id"]),
        )

        agent_prompt = await self._build_agent_prompt(
            sub_task, todo, previous_results,
            workspace_path=workspace_path, agent_config=agent_config,
        )

        # Resolve MCP tools and skills for this project
        mcp_tools = await self.tools_registry.resolve_tools(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )
        skills_context = await self.tools_registry.build_skills_context(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )

        # Filter tools if custom agent has tools_enabled
        if agent_config and agent_config.get("tools_enabled"):
            allowed = set(agent_config["tools_enabled"])
            if mcp_tools:
                mcp_tools = [t for t in mcp_tools if t.get("name") in allowed]

        # Ensure agents always have workspace tools (built-in fallback)
        if workspace_path:
            builtin_tools = self._get_builtin_tools(workspace_path, sub_task["agent_role"])
            # Inject shared per-project index directory for semantic_search
            _exec_idx = os.path.normpath(os.path.join(workspace_path, "..", ".agent_index"))
            for _bt in builtin_tools:
                if _bt["name"] == "semantic_search":
                    _bt["_index_dir"] = _exec_idx
            if mcp_tools:
                existing_names = {t["name"] for t in mcp_tools}
                mcp_tools.extend(t for t in builtin_tools if t["name"] not in existing_names)
            else:
                mcp_tools = builtin_tools

        # Append skills context to system prompt
        system_prompt = agent_prompt["system"]
        if skills_context:
            system_prompt += skills_context

        # Model preference from custom agent
        model_override = agent_config.get("model_preference") if agent_config else None

        try:
            # Report progress
            await self._report_progress(st_id, 10, f"Starting: {sub_task['title']}")

            messages = [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=agent_prompt["user"]),
            ]
            tools_arg = mcp_tools if mcp_tools else None

            from agents.providers.base import run_tool_loop

            logger.info("[%s] st=%s Sending LLM request (tools=%d, model_override=%s)",
                        self.todo_id, st_id, len(tools_arg) if tools_arg else 0, model_override)

            # Roles that write code need more output tokens for write_file calls
            role = sub_task["agent_role"]
            role_max_tokens = 16384 if role in ("coder", "tester") else 8192

            send_kwargs: dict = {"temperature": 0.1, "max_tokens": role_max_tokens}
            if model_override:
                send_kwargs["model"] = model_override

            async def _on_tool_round(round_num: int, resp: LLMResponse) -> None:
                await self._report_progress(
                    st_id, 10 + round_num * 6,
                    f"Using tool: {resp.tool_calls[0].get('name', '?') if resp.tool_calls else '?'}",
                )

            async def _tool_exec(name: str, args: dict) -> str:
                if name == "create_subtask":
                    return await self._handle_create_subtask_tool(
                        sub_task, args, workspace_path,
                    )
                return await self.mcp_executor.execute_tool(name, args, mcp_tools)

            content, response = await run_tool_loop(
                provider, messages,
                tools=tools_arg,
                tool_executor=_tool_exec,
                max_rounds=10,
                on_tool_round=_on_tool_round,
                on_activity=lambda msg: self._report_activity(st_id, msg),
                **send_kwargs,
            )
            logger.info("[%s] st=%s Tool loop done: content_len=%d stop=%s",
                        self.todo_id, st_id, len(content) if content else 0, response.stop_reason)

            await self._report_progress(st_id, 80, f"Processing output: {sub_task['title']}")

            # ── Structured output validation + retry ──
            from agents.orchestrator.output_validator import (
                validate_agent_output,
                build_correction_prompt,
                MAX_VALIDATION_RETRIES,
            )

            validated_output = None
            for val_attempt in range(MAX_VALIDATION_RETRIES + 1):
                validated_output, val_errors = validate_agent_output(
                    sub_task["agent_role"], response.content,
                )
                if validated_output is not None:
                    break
                if val_attempt < MAX_VALIDATION_RETRIES:
                    correction = build_correction_prompt(
                        sub_task["agent_role"], val_errors, response.content,
                    )
                    messages.append(LLMMessage(role="assistant", content=response.content))
                    messages.append(LLMMessage(role="user", content=correction))
                    response = await provider.send_message(
                        messages, temperature=0.1, tools=tools_arg,
                        **({"model": model_override} if model_override else {}),
                    )

            if validated_output is None:
                logger.warning(
                    "Subtask %s (%s) failed output validation after %d retries: %s",
                    st_id, sub_task["agent_role"], MAX_VALIDATION_RETRIES, val_errors,
                )
                validated_output = {"content": response.content, "raw_content": response.content}

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Update agent run
            await self.db.execute(
                """
                UPDATE agent_runs
                SET status = 'completed', output_result = $2,
                    tokens_input = $3, tokens_output = $4,
                    duration_ms = $5, cost_usd = $6, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                validated_output,
                response.tokens_input,
                response.tokens_output,
                duration_ms,
                response.cost_usd,
            )

            # Update sub-task
            await self._transition_subtask(
                st_id,
                "completed",
                progress_pct=100,
                progress_message="Done",
                output_result=validated_output,
            )

            # Create deliverable if the agent produced one
            await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)

            # Update token tracking on the TODO
            await self.db.execute(
                """
                UPDATE todo_items
                SET actual_tokens = actual_tokens + $2,
                    cost_usd = cost_usd + $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                self.todo_id,
                response.tokens_input + response.tokens_output,
                response.cost_usd,
            )

            await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")

        except Exception as e:
            import traceback as _tb

            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_type = _classify_error(e)
            error_detail = f"{type(e).__name__}: {e}"
            error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

            await self.db.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error_type = $2,
                    error_detail = $3, duration_ms = $4, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                error_type,
                error_detail + "\n\n" + error_traceback[-2000:],
                duration_ms,
            )
            raise

    # ---- REVIEW PHASE ----

    async def _phase_review(self, todo: dict, provider: AIProvider) -> None:
        """Auto-review deliverables. Only notify human if issues found."""
        # Re-read current state to avoid stale transitions (e.g. concurrent dispatch)
        current = await self.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", self.todo_id,
        )
        if current == "review":
            logger.info("[%s] Already in review state, skipping transition", self.todo_id)
        elif current == "in_progress":
            await self._transition_todo( "review")
        else:
            logger.warning("[%s] Cannot enter review from state=%s, skipping review phase", self.todo_id, current)
            return

        # Gather all results
        results = await self._get_completed_results()

        messages = [
            LLMMessage(role="system", content=REVIEW_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Original task: {todo['title']}\n"
                    f"Description: {todo['description']}\n"
                    f"Requirements: {json.dumps(safe_json(todo.get('intake_data')), default=str)}\n\n"
                    f"Completed sub-task results:\n"
                    + "\n---\n".join(
                        f"[{r.get('agent_role', '?')}] {r.get('title', '?')}: "
                        f"{json.dumps(r.get('output_result', {}), default=str)[:2000]}"
                        for r in results
                    )
                ),
            ),
        ]

        response = await provider.send_message(
            messages, model=provider.get_model(use_fast=True), temperature=0.1
        )
        await self._track_tokens(response)

        review = parse_llm_json(response.content)
        if review is None:
            review = {"approved": True, "summary": response.content}

        # Try to commit, push, and create PR if workspace exists
        pr_info = await self._finalize_workspace(todo, review.get("summary", ""))

        summary = review.get("summary", "Completed successfully")
        if pr_info:
            summary += f"\n\nPR created: {pr_info.get('url', 'N/A')}"

        approved = review.get("approved", True)
        issues = review.get("issues", [])

        if approved:
            issue_note = ""
            if issues:
                issue_note = "\n\n**Minor notes:**\n" + "\n".join(f"- {i}" for i in issues)
            await self._post_system_message(
                f"**Review passed.** {review.get('summary', '')}\n\n"
                + (f"**PR:** {pr_info['url']}\n\n" if pr_info else "")
                + "Task completed."
                + issue_note
            )
            await self._transition_todo(
                "completed",
                result_summary=summary,
            )
            await self.notifier.notify(
                str(todo["creator_id"]),
                "completed",
                {
                    "todo_id": self.todo_id,
                    "title": todo["title"],
                    "detail": summary,
                },
            )
        else:
            # Review found real issues — still complete but flag them
            issue_text = "\n".join(f"- {i}" for i in issues) if issues else "No specific issues."
            await self._post_system_message(
                f"**Review complete with issues:**\n\n"
                f"{review.get('summary', '')}\n\n"
                f"**Issues found:**\n{issue_text}\n\n"
                + (f"**PR:** {pr_info['url']}\n\n" if pr_info else "")
                + "Task completed. Review the issues above and create follow-up tasks if needed."
            )
            await self._transition_todo(
                "completed",
                result_summary=f"{summary}\n\nIssues: {issue_text}",
            )
            await self.notifier.notify(
                str(todo["creator_id"]),
                "completed",
                {
                    "todo_id": self.todo_id,
                    "title": todo["title"],
                    "detail": f"Completed with issues: {review.get('summary', '')}",
                },
            )

        # Extract and store persistent memories from completed task
        try:
            from agents.indexing.memory_extractor import extract_memories, deduplicate_memories
            # Gather iteration logs from all subtasks
            all_iter_logs = []
            for r in results:
                if r.get("iteration_log"):
                    log = r["iteration_log"]
                    if isinstance(log, str):
                        import json as _json
                        log = _json.loads(log)
                    all_iter_logs.extend(log)

            if all_iter_logs:
                memories = await extract_memories(
                    all_iter_logs,
                    task_title=todo["title"],
                    task_summary=summary,
                    provider=provider,
                )
                if memories:
                    # Fetch existing memories for dedup
                    existing = await self.db.fetch(
                        "SELECT content FROM project_memories WHERE project_id = $1",
                        todo["project_id"],
                    )
                    existing_contents = [row["content"] for row in existing]
                    unique_memories = await deduplicate_memories(memories, existing_contents)

                    for mem in unique_memories:
                        await self.db.execute(
                            """INSERT INTO project_memories
                               (project_id, category, content, source_todo_id, confidence)
                               VALUES ($1, $2, $3, $4, $5)""",
                            todo["project_id"], mem.category, mem.content,
                            self.todo_id, mem.confidence,
                        )
                    logger.info(
                        "[%s] Stored %d new project memories (extracted %d, %d deduplicated)",
                        self.todo_id, len(unique_memories), len(memories),
                        len(memories) - len(unique_memories),
                    )
        except ImportError:
            logger.debug("[%s] memory_extractor not available, skipping memory extraction", self.todo_id)
        except Exception as e:
            logger.warning("[%s] Failed to extract project memories: %s", self.todo_id, e)

    # ---- WORKSPACE FINALIZATION ----

    async def _finalize_workspace(self, todo: dict, summary: str) -> dict | None:
        """Commit changes, push branch, and create PR if workspace has changes."""
        try:
            project = await self.db.fetchrow(
                "SELECT repo_url, default_branch FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if not project or not project.get("repo_url"):
                return None

            # Find the task workspace
            project_dir = os.path.join(settings.workspace_root, str(todo["project_id"]))
            task_dir = os.path.join(project_dir, "tasks", str(self.todo_id))
            if not os.path.isdir(task_dir):
                return None

            short_id = str(self.todo_id)[:8]
            branch_name = f"task/{short_id}"
            base_branch = project.get("default_branch") or "main"

            # Commit and push
            pushed = await self.workspace_mgr.commit_and_push(
                task_dir,
                message=f"[agents] {todo['title']}\n\n{summary}",
                branch=branch_name,
            )
            if not pushed:
                return None

            # Create PR
            pr_info = await self.workspace_mgr.create_pr(
                str(todo["project_id"]),
                head_branch=branch_name,
                base_branch=base_branch,
                title=todo["title"],
                body=(
                    f"## Summary\n{summary}\n\n"
                    f"---\n*Created by AI Agent Orchestrator*"
                ),
            )

            # Store PR as deliverable
            if pr_info:
                await self.db.execute(
                    """
                    INSERT INTO deliverables (
                        todo_id, type, title, pr_url, pr_number, branch_name, status
                    )
                    VALUES ($1, 'pull_request', $2, $3, $4, $5, 'pending')
                    """,
                    self.todo_id,
                    f"PR: {todo['title']}",
                    pr_info.get("url"),
                    pr_info.get("number"),
                    branch_name,
                )

            return pr_info

        except Exception:
            logger.warning("Failed to finalize workspace for %s", self.todo_id, exc_info=True)
            return None

    # ---- DEPENDENCY WORKSPACE ----

    async def _setup_dependency_workspace(self, sub_task: dict) -> str:
        """Set up a separate workspace for a sub-task targeting a dependency repo.

        The workspace includes:
        - repo/ — the cloned dependency repo (writable)
        - deps/ — symlink to project-level deps for cross-repo reads
        - main_repo/ — symlink to the main project repo for reference (read-only)
        """
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo)
        if not target_repo or not target_repo.get("repo_url"):
            raise ValueError("No target_repo configured on sub-task")

        todo = await self._load_todo()
        project_dir = os.path.join(
            settings.workspace_root, str(todo["project_id"]),
        )
        dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
        dep_task_dir = os.path.join(
            project_dir, "tasks", str(self.todo_id), f"dep_{dep_name}",
        )
        dep_repo_dir = os.path.join(dep_task_dir, "repo")

        if os.path.isdir(dep_repo_dir):
            return dep_task_dir

        os.makedirs(dep_task_dir, exist_ok=True)

        # Resolve credentials
        git_provider_id = target_repo.get("git_provider_id")
        token, provider_type = await self.workspace_mgr._resolve_git_credentials(
            git_provider_id, target_repo["repo_url"],
        )

        from agents.orchestrator.git_providers.factory import build_clone_url
        clone_url = build_clone_url(target_repo["repo_url"], token, provider_type)
        branch = target_repo.get("default_branch") or "main"

        rc, out = await self.workspace_mgr._run_git(
            "clone", "--depth", "1", "--branch", branch, clone_url, dep_repo_dir,
            cwd=self.workspace_mgr.workspace_root,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone dependency repo: {out}")

        # Create task branch
        short_id = str(self.todo_id)[:8]
        task_branch = f"task/{short_id}-{dep_name}"
        await self.workspace_mgr._run_git(
            "checkout", "-b", task_branch, cwd=dep_repo_dir,
        )

        # Symlink project-level deps dir for cross-repo reads
        project_deps_dir = os.path.join(project_dir, "deps")
        dep_deps_link = os.path.join(dep_task_dir, "deps")
        if os.path.isdir(project_deps_dir) and not os.path.exists(dep_deps_link):
            try:
                os.symlink(project_deps_dir, dep_deps_link)
            except OSError:
                logger.debug("Could not symlink deps into dep workspace")

        # Symlink main project repo for reference
        main_repo_dir = os.path.join(project_dir, "repo")
        main_repo_link = os.path.join(dep_task_dir, "main_repo")
        if os.path.isdir(main_repo_dir) and not os.path.exists(main_repo_link):
            try:
                os.symlink(main_repo_dir, main_repo_link)
            except OSError:
                logger.debug("Could not symlink main repo into dep workspace")

        return dep_task_dir

    # ---- HELPERS ----

    async def _load_todo(self) -> dict:
        row = await self.db.fetchrow("SELECT * FROM todo_items WHERE id = $1", self.todo_id)
        return dict(row)

    async def _build_context(self, todo: dict) -> dict:
        project = await self.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", todo["project_id"]
        )
        context = {
            "project_name": project["name"] if project else "Unknown",
            "repo_url": project["repo_url"] if project else None,
            "default_branch": project["default_branch"] if project else "main",
            "project_description": project["description"] if project else None,
        }
        # Include project dependencies for richer AI context
        if project and project.get("context_docs"):
            deps = project["context_docs"]
            if isinstance(deps, str):
                deps = json.loads(deps)
            context["dependencies"] = deps

            # Build dependency_dirs mapping (name -> normalized dir name) for agents
            dep_dirs = {}
            for dep in deps:
                name = dep.get("name", "")
                if name:
                    dir_name = name.replace("/", "_").replace(" ", "_")
                    dep_dirs[name] = dir_name
            if dep_dirs:
                context["dependency_dirs"] = dep_dirs

        # Include stored project understanding from analysis
        if project and project.get("settings_json"):
            settings = project["settings_json"]
            if isinstance(settings, str):
                settings = json.loads(settings)
            understanding = settings.get("project_understanding")
            if understanding:
                context["project_understanding"] = understanding
            # Include debug context so the planner knows debugging capabilities
            debug_ctx = settings.get("debug_context")
            if debug_ctx:
                context["debug_context"] = debug_ctx

        return context

    async def _load_chat_history(self) -> list[dict]:
        session_id = getattr(self, "_chat_session_id", None)
        if session_id:
            rows = await self.db.fetch(
                "SELECT role, content FROM project_chat_messages "
                "WHERE session_id = $1 ORDER BY created_at ASC",
                session_id,
            )
        else:
            rows = await self.db.fetch(
                "SELECT role, content FROM chat_messages WHERE todo_id = $1 ORDER BY created_at ASC",
                self.todo_id,
            )
        return [dict(r) for r in rows]

    async def _check_for_user_messages(self) -> str | None:
        msg = await self.redis.lpop(f"task:{self.todo_id}:chat_input")
        return msg

    async def _handle_user_message(self, message: str, provider: AIProvider) -> None:
        todo = await self._load_todo()
        chat_history = await self._load_chat_history()

        messages = [
            LLMMessage(
                role="system",
                content=(
                    f"You are the coordinator for task: {todo['title']}.\n"
                    f"Current state: {todo['state']} / {todo.get('sub_state', 'N/A')}.\n"
                    "The user sent a message. Respond helpfully and concisely. "
                    "If they want changes, acknowledge and note them for the next execution cycle."
                ),
            ),
        ]
        for msg in chat_history[-20:]:  # last 20 messages for context
            messages.append(LLMMessage(role=msg["role"], content=msg["content"]))
        messages.append(LLMMessage(role="user", content=message))

        response = await provider.send_message(
            messages, model=provider.get_model(use_fast=True)
        )
        await self._track_tokens(response)

        await self._post_assistant_message(response.content)

    async def _post_system_message(self, content: str) -> None:
        await self._post_chat_message("system", content)

    async def _post_assistant_message(self, content: str) -> None:
        await self._post_chat_message("assistant", content)

    async def _post_chat_message(self, role: str, content: str) -> None:
        """Write a chat message to the correct table and publish to relevant channels."""
        session_id = getattr(self, "_chat_session_id", None)

        if session_id:
            # Linked session: write to project_chat_messages
            await self.db.execute(
                "INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id) "
                "VALUES ($1, $2, $3, $4, $5)",
                self._chat_project_id, self._chat_user_id, role, content, session_id,
            )
        else:
            # Standard: write to chat_messages
            await self.db.execute(
                "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, $2, $3)",
                self.todo_id, role, content,
            )

        # Always publish to the task events channel for the todo detail WS
        event_data = json.dumps({
            "type": "chat_message",
            "message": {"role": role, "content": content},
        })
        await self.redis.publish(f"task:{self.todo_id}:events", event_data)
        # Also publish to session channel if linked
        if session_id:
            await self.redis.publish(f"chat:session:{session_id}:activity", event_data)

    async def _report_progress(self, subtask_id: str, pct: int, message: str) -> None:
        logger.info("[%s] st=%s progress=%d%% %s", self.todo_id, subtask_id[:8], pct, message)
        # Persist to DB so it survives page refreshes
        await self.db.execute(
            "UPDATE sub_tasks SET progress_pct = $2, progress_message = $3 WHERE id = $1",
            subtask_id, pct, message,
        )
        await self.redis.publish(
            f"task:{self.todo_id}:progress",
            json.dumps({
                "type": "progress",
                "sub_task_id": subtask_id,
                "progress_pct": pct,
                "message": message,
            }),
        )

    async def _report_activity(self, subtask_id: str, activity: str) -> None:
        """Publish a granular activity event for live UI display.

        Throttled to avoid flooding WebSocket clients:
        - Redis publish: at most once per second per subtask
        - DB persist: at most once per 3 seconds per subtask
        """
        import time

        now = time.monotonic()

        # Persist to DB at most every 3s
        last_persist = self._last_activity_persist.get(subtask_id, 0)
        if now - last_persist >= 3.0:
            await self.db.execute(
                "UPDATE sub_tasks SET progress_message = $2 WHERE id = $1",
                subtask_id, activity,
            )
            self._last_activity_persist[subtask_id] = now

        # Publish to Redis at most every 1s
        last_publish = self._last_activity_publish.get(subtask_id, 0)
        if now - last_publish >= 1.0:
            await self.redis.publish(
                f"task:{self.todo_id}:progress",
                json.dumps({
                    "type": "activity",
                    "sub_task_id": subtask_id,
                    "activity": activity,
                }),
            )
            self._last_activity_publish[subtask_id] = now

    async def _report_planning_activity(self, activity: str) -> None:
        """Publish a planning-phase activity event for live UI display (throttled)."""
        import time

        now = time.monotonic()
        last = self._last_activity_publish.get("_planning", 0)
        if now - last < 1.0:
            return
        self._last_activity_publish["_planning"] = now

        await self.redis.publish(
            f"task:{self.todo_id}:progress",
            json.dumps({
                "type": "activity",
                "phase": "planning",
                "activity": activity,
            }),
        )

    async def _get_completed_results(self) -> list[dict]:
        rows = await self.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'completed'",
            self.todo_id,
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _get_builtin_tools(workspace_path: str, role: str = "coder") -> list[dict]:
        """Return role-appropriate built-in workspace tool definitions.

        Delegates to the canonical BUILTIN_TOOLS registry in agents.agents.registry.
        These tools are executed directly by McpToolExecutor._execute_builtin()
        rather than going through an external MCP server.
        """
        return get_builtin_tool_schemas(workspace_path, role)

    async def _track_tokens(self, response: LLMResponse) -> None:
        await self.db.execute(
            "UPDATE todo_items SET actual_tokens = actual_tokens + $2, "
            "cost_usd = cost_usd + $3, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            response.tokens_input + response.tokens_output,
            response.cost_usd,
        )

    async def _get_workspace_diff(self, workspace_path: str) -> dict | None:
        """Get git diff from workspace after coder commits."""
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        async def _run(args):
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode, stdout.decode(errors="replace")

        # Unified diff of last commit
        rc, diff_output = await _run(["diff", "HEAD~1", "HEAD"])
        if rc != 0 or not diff_output.strip():
            return None

        # Diffstat
        _, stat_output = await _run(["diff", "--stat", "HEAD~1", "HEAD"])

        # Changed files with status
        _, files_output = await _run(["diff", "--name-status", "HEAD~1", "HEAD"])
        files = []
        for line in files_output.strip().split("\n"):
            if line.strip():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    files.append({"status": parts[0], "path": parts[1]})

        return {
            "diff": diff_output[:500_000],  # cap at 500KB
            "stats": stat_output.strip(),
            "files": files,
        }

    async def _maybe_create_deliverable(
        self, sub_task: dict, response: LLMResponse, run: dict,
        *, workspace_path: str | None = None,
    ) -> None:
        """Create a deliverable if the agent role produces one."""
        role = sub_task["agent_role"]
        if role in ("coder", "report_writer"):
            d_type = {
                "coder": "code_diff",
                "report_writer": "report",
            }.get(role, "document")

            # For coders: capture actual git diff from workspace
            diff_json = None
            if role == "coder" and workspace_path:
                try:
                    diff_json = await self._get_workspace_diff(workspace_path)
                except Exception:
                    logger.warning("Failed to capture git diff for deliverable", exc_info=True)

            await self.db.execute(
                """
                INSERT INTO deliverables (
                    todo_id, agent_run_id, sub_task_id, type, title, content_md, content_json
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                self.todo_id,
                run["id"],
                sub_task["id"],
                d_type,
                f"{d_type}: {sub_task['title']}",
                response.content,
                diff_json,
            )

    async def _build_agent_prompt(
        self, sub_task: dict, todo: dict, previous_results: list[dict],
        *, workspace_path: str | None = None, work_rules: dict | None = None,
        agent_config: dict | None = None,
    ) -> dict:
        """Build system and user prompts for a specialist agent.

        If an agent_config is provided (custom agent), its system_prompt is used
        instead of the hardcoded default for the role.
        """
        role = sub_task["agent_role"]
        intake = safe_json(todo.get("intake_data"))

        # Determine if this is a dependency repo sub-task
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        is_dep_workspace = bool(target_repo and target_repo.get("repo_url"))

        # Build workspace context if available
        workspace_context = ""
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=4)

            if is_dep_workspace:
                dep_name = target_repo.get("name", "dependency")
                workspace_context = (
                    f"\n\nYou are working inside the DEPENDENCY repository: {dep_name}\n"
                    f"This is a dependency of the main project. Your changes will create a PR "
                    f"on this dependency's repo.\n"
                    f"Repository file structure:\n{file_tree}\n"
                )
                # Main repo available for reference
                main_repo_dir = os.path.join(workspace_path, "main_repo")
                if os.path.isdir(main_repo_dir):
                    workspace_context += (
                        "\nThe main project repo is available for reference (read-only) at "
                        "../main_repo/ relative to repo root.\n"
                        "Use read_file('../main_repo/path') to see how the main project "
                        "uses this dependency.\n"
                    )
            else:
                workspace_context = (
                    f"\n\nYou are working inside the project repository root directory.\n"
                    f"Project file structure:\n{file_tree}\n"
                )

            # List available dependency repos for reference
            task_deps_dir = os.path.join(workspace_path, "deps")
            if os.path.isdir(task_deps_dir):
                dep_entries = sorted(os.listdir(task_deps_dir))
                if dep_entries:
                    workspace_context += (
                        "\nDependency repositories available for reference (read-only):\n"
                        "Access via ../deps/{name}/path relative to repo root.\n"
                    )
                    for d in dep_entries:
                        full = os.path.join(task_deps_dir, d)
                        if os.path.isdir(full):
                            workspace_context += f"  - ../deps/{d}/\n"

        # Work rules injection
        rules_block = ""
        if work_rules:
            filtered = self._filter_rules_for_role(work_rules, role)
            rules_block = self._format_rules_for_prompt(filtered)

        # Use custom agent prompt if available, otherwise default
        if agent_config and agent_config.get("system_prompt"):
            system = agent_config["system_prompt"] + workspace_context
        else:
            system = get_default_system_prompt(role) + workspace_context

        system += rules_block

        # For debugger agents, inject project-level debug context
        if role == "debugger":
            debug_block = await self._build_debug_context_block(todo)
            if debug_block:
                system += debug_block

        # For tester agents, inject project build/test commands
        if role == "tester":
            build_block = await self._build_tester_context(todo)
            if build_block:
                system += build_block

        # Inject tool descriptions into prompt so all models know what's available
        if workspace_path:
            system += build_tools_prompt_block(role)

        from agents.orchestrator.output_validator import build_structured_output_instruction
        system += build_structured_output_instruction(role)

        prev_context = ""
        if previous_results:
            prev_items = []
            for r in previous_results:
                out = r.get("output_result", {})
                summary = out.get("approach", "") or out.get("summary", "") if isinstance(out, dict) else ""
                prev_items.append(
                    f"- [{r.get('agent_role', '?')}] {r.get('title', '?')}: {summary[:300]}"
                )
            prev_context = "\n\nCompleted sub-tasks:\n" + "\n".join(prev_items)

        # Inject previous run context if this is a retry-with-context
        previous_run = intake.get("previous_run")
        if previous_run:
            prev_run_ctx = f"\n\nRETRY — previous outcome: {previous_run.get('previous_state', 'unknown')}\n"
            if previous_run.get("result_summary"):
                prev_run_ctx += f"{previous_run['result_summary']}\n"
            for pst in previous_run.get("sub_tasks", []):
                prev_run_ctx += f"- [{pst.get('role', '?')}] {pst.get('title', '?')}: {pst.get('status', '?')}"
                if pst.get("error"):
                    prev_run_ctx += f" (error: {pst['error']})"
                prev_run_ctx += "\n"
            prev_context += prev_run_ctx

        return {
            "system": system,
            "user": (
                f"Task: {todo['title']}\n"
                f"Sub-task: {sub_task['title']}\n"
                f"Description: {sub_task['description'] or 'N/A'}\n"
                f"Requirements: {json.dumps(intake, default=str)}\n"
                f"{prev_context}"
            ),
        }

    # ---- DEBUG CONTEXT ----

    async def _build_debug_context_block(self, todo: dict) -> str:
        """Build a markdown block with debug context for debugger agents.

        Pulls log sources, MCP data hints, and custom instructions from
        the project's settings_json.debug_context. Also checks dependency-level
        debug contexts from context_docs.
        """
        project = await self.db.fetchrow(
            "SELECT settings_json, context_docs FROM projects WHERE id = $1",
            todo["project_id"],
        )
        if not project:
            return ""

        settings = project.get("settings_json") or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        debug_ctx = settings.get("debug_context") or {}

        parts: list[str] = []

        # Log sources
        log_sources = debug_ctx.get("log_sources") or []
        if log_sources:
            parts.append("\n\n## Log Sources")
            for src in log_sources:
                parts.append(f"### {src.get('service_name', 'Service')}")
                if src.get("log_path"):
                    parts.append(f"- **Log path:** `{src['log_path']}`")
                if src.get("log_command"):
                    parts.append(f"- **Log command:** `{src['log_command']}`")
                if src.get("description"):
                    parts.append(f"- **Description:** {src['description']}")

        # MCP data hints
        mcp_hints = debug_ctx.get("mcp_hints") or []
        if mcp_hints:
            parts.append("\n\n## MCP Data Sources")
            for hint in mcp_hints:
                name = hint.get("mcp_server_name", "MCP Server")
                parts.append(f"### {name}")
                available = hint.get("available_data") or []
                if available:
                    parts.append("**Available data:** " + ", ".join(available))
                queries = hint.get("example_queries") or []
                if queries:
                    parts.append("**Example queries:**")
                    for q in queries:
                        parts.append(f"  ```\n  {q}\n  ```")
                if hint.get("notes"):
                    parts.append(f"**Notes:** {hint['notes']}")

        # Custom instructions
        custom = debug_ctx.get("custom_instructions") or ""
        if custom:
            parts.append(f"\n\n## Debug Instructions\n{custom}")

        # Dependency-level debug contexts
        deps = project.get("context_docs") or []
        if isinstance(deps, str):
            deps = json.loads(deps)
        for dep in deps:
            dep_debug = dep.get("debug_context") if isinstance(dep, dict) else None
            if dep_debug:
                dep_name = dep.get("name", "Dependency")
                parts.append(f"\n\n## Debug Context: {dep_name}")
                for src in dep_debug.get("log_sources", []):
                    if src.get("log_path"):
                        parts.append(f"- Log: `{src['log_path']}`")
                    if src.get("log_command"):
                        parts.append(f"- Command: `{src['log_command']}`")
                for hint in dep_debug.get("mcp_hints", []):
                    parts.append(f"- MCP: {hint.get('mcp_server_name', '?')} — {', '.join(hint.get('available_data', []))}")
                if dep_debug.get("custom_instructions"):
                    parts.append(f"- Instructions: {dep_debug['custom_instructions']}")

        if not parts:
            parts.append(
                "\n\n## Debug Context\n"
                "No debug context is configured for this project. "
                "Use codebase exploration, error messages, and available MCP tools "
                "to investigate the issue."
            )

        return "\n".join(parts)

    async def _build_tester_context(self, todo: dict) -> str:
        """Build a context block with build/test commands for tester agents.

        If the project has configured build_commands, inject them.
        Otherwise, provide discovery instructions.
        """
        project = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1",
            todo["project_id"],
        )
        settings = safe_json(project.get("settings_json")) if project else {}
        build_commands = settings.get("build_commands", [])

        if build_commands:
            cmds = "\n".join(f"  - `{cmd}`" for cmd in build_commands)
            return (
                f"\n\n## Project Build & Test Commands\n"
                f"The project has these configured build/test commands:\n{cmds}\n"
                f"You MUST run these commands to validate the implementation. "
                f"If any fail, investigate and fix the issues before reporting success."
            )
        else:
            return (
                "\n\n## Build & Test Discovery\n"
                "No build/test commands are configured for this project. "
                "Discover them by checking:\n"
                "- `package.json` → `scripts.test`, `scripts.build`, `scripts.lint`\n"
                "- `Makefile` or `Taskfile.yml`\n"
                "- `pyproject.toml` / `setup.py` / `tox.ini`\n"
                "- `Cargo.toml` (cargo test)\n"
                "- CI config files (`.github/workflows/`, `.gitlab-ci.yml`)\n\n"
                "Run the discovered test/build commands to validate the implementation."
            )

    # ---- CODE PUSH (PR + MERGE) ----

    async def _ensure_code_push(self, workspace_path: str | None) -> bool:
        """Create a pr_creator subtask if coder work completed without a PR.

        Called after all subtasks complete successfully, before _phase_review.
        Skips if a PR already exists (e.g. created by the review-loop flow)
        or if a pr_creator subtask is already pending/running.

        Returns True if a pr_creator subtask was created (caller should execute it).
        """
        if not workspace_path:
            return False

        # Skip if a PR deliverable already exists (review-loop already handled it)
        existing_pr = await self.db.fetchrow(
            "SELECT id FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
            self.todo_id,
        )
        if existing_pr:
            return False

        # Skip if a pr_creator subtask already exists
        existing_pr_task = await self.db.fetchrow(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
            "AND status IN ('pending', 'assigned', 'running')",
            self.todo_id,
        )
        if existing_pr_task:
            return False

        # Check for completed coder subtasks (the ones that produced code)
        coder_subtasks = await self.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
            "AND status = 'completed' ORDER BY created_at DESC",
            self.todo_id,
        )
        if not coder_subtasks:
            return False

        all_coder_ids = [str(st["id"]) for st in coder_subtasks]
        latest_coder = coder_subtasks[0]
        target_repo_json = latest_coder.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        max_order = await self.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            self.todo_id,
        )

        await self._post_system_message(
            "**Code push:** Creating PR sub-task for completed code changes..."
        )

        await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6)
            RETURNING id
            """,
            self.todo_id,
            "Create Pull Request",
            "Commit all workspace changes, push to a feature branch, and create a pull request.",
            max_order + 1,
            all_coder_ids,
            target_repo_json,
        )

        return True

    # ---- CANCELLATION CHECK ----

    async def _is_cancelled(self) -> bool:
        """Check if the todo has been cancelled (e.g., by the user via the API)."""
        state = await self.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", self.todo_id,
        )
        return state == "cancelled"

