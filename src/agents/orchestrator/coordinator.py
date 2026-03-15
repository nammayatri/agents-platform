"""Agent Coordinator: the per-task brain.

Each TODO item gets one AgentCoordinator instance. It manages:
1. INTAKE: AI interviewer that asks thorough questions upfront
2. PLANNING: Decomposes task into sub-tasks with agent assignments
3. EXECUTION: Runs sub-tasks in parallel (respecting dependencies)
4. REVIEW: Auto-reviews deliverables, only escalates if needed
5. CHAT: Handles user messages between steps for steering
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
    transition_subtask,
    transition_todo,
)
from agents.providers.base import AIProvider
from agents.providers.mcp_executor import McpToolExecutor
from agents.providers.registry import ProviderRegistry
from agents.providers.tools_registry import ToolsRegistry
from agents.schemas.agent import LLMMessage, LLMResponse
from agents.utils.json_helpers import extract_json, fix_trailing_commas, parse_llm_json, safe_json

logger = logging.getLogger(__name__)

from agents.agents.registry import (
    build_tools_prompt_block,
    get_agent_definition,
    get_builtin_tool_schemas,
    get_default_system_prompt,
    get_default_tools,
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
You are a task planner. Decompose the task into sub-tasks for specialist agents.

Sub-task fields: title, description, agent_role, execution_order (0=parallel), \
depends_on (0-based indexes), review_loop (true for code changes needing coder→reviewer→merge cycle), \
target_repo (optional: {"repo_url":"...", "name":"...", "default_branch":"main", "git_provider_id":null}).

Maximize parallelism. Output ONLY JSON:
{"summary":"...", "sub_tasks":[{"title":"...", "description":"...", "agent_role":"...", \
"execution_order":0, "depends_on":[], "review_loop":false, "target_repo":null}], "estimated_tokens":5000}
"""

REVIEW_SYSTEM_PROMPT = """\
You are a work reviewer. Approve unless there are objective problems (broken logic, \
missing critical requirements, security issues). Style nits are not blockers.

Output JSON: {"approved": true/false, "issues": [...], "summary": "..."}
"""


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
                    logger.warning("[%s] plan_ready but no plan_json! Will re-plan on next cycle",
                                   self.todo_id)
            case "in_progress":
                logger.info("[%s] → entering _phase_execution", self.todo_id)
                await self._phase_execution(todo, provider)
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
            # If the LLM didn't output clean JSON, default to ready — don't block on bad parsing
            logger.warning("Intake: failed to parse JSON, defaulting to ready=true")
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
                "UPDATE todo_items SET intake_data = $2::jsonb WHERE id = $1",
                self.todo_id,
                json.dumps(intake_data),
            )
            await self._post_system_message(
                f"**Intake complete.** Moving to planning.\n\n"
                f"**Requirements:** {json.dumps(result.get('requirements', {}), indent=2)}\n\n"
                f"**Approach:** {result.get('approach', 'Auto-determined')}"
            )
            await transition_todo(self.db, self.todo_id, "planning", sub_state="decomposing")
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
        """Decompose task into sub-tasks and park for human approval."""
        if todo["state"] != "planning":
            await transition_todo(self.db, self.todo_id, "planning", sub_state="decomposing")
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
        available_roles = "coder, tester, reviewer, pr_creator, report_writer, merge_agent"
        if custom_agents:
            custom_lines = [f"  - {a['role']}: {a['name']} — {a['description'] or 'no description'}" for a in custom_agents]
            available_roles += "\n\nCustom agents available:\n" + "\n".join(custom_lines)

        planner_prompt = PLANNER_SYSTEM_PROMPT + f"\n\nAvailable agent roles: {available_roles}\n"

        messages = [
            LLMMessage(role="system", content=planner_prompt),
            LLMMessage(
                role="user",
                content=(
                    f"Task: {todo['title']}\n"
                    f"Description: {todo['description'] or 'N/A'}\n"
                    f"Type: {todo['task_type']}\n"
                    f"Intake data: {json.dumps(intake_data, default=str)}\n"
                    f"Project context: {json.dumps(context, default=str)}"
                ),
            ),
        ]

        plan = None
        max_retries = 3
        for attempt in range(max_retries):
            response = await provider.send_message(messages, temperature=0.1)
            await self._track_tokens(response)

            # Try parsing the response as JSON (with trailing comma fix)
            plan = parse_llm_json(response.content)
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
                                   self.todo_id, response.content)
                break

            # Parse failed — retry with correction prompt (don't keep bad response in context)
            if attempt < max_retries - 1:
                logger.warning(
                    "Plan parse attempt %d/%d failed for todo %s, retrying",
                    attempt + 1, max_retries, self.todo_id,
                )
                # Replace messages with fresh context + correction instruction
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
                # Final attempt failed
                await self._post_system_message(
                    "**Planning failed** — could not parse plan after multiple attempts. Retrying on next cycle."
                )
                raise ValueError("Failed to parse execution plan from LLM after retries")

        # Store plan as structured JSON for human review
        await self.db.execute(
            "UPDATE todo_items SET plan_json = $2::jsonb, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            json.dumps(plan),
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
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb)
                    RETURNING id
                    """,
                    self.todo_id,
                    st["title"],
                    st.get("description", ""),
                    st["agent_role"],
                    st.get("execution_order", 0),
                    json.dumps(st.get("context", {})),
                    bool(st.get("review_loop", False)),
                    json.dumps(target_repo) if target_repo else None,
                )
                st_id = str(row["id"])
                sub_task_ids.append(st_id)
                logger.info("[%s] Inserted sub_task[%d] id=%s", self.todo_id, i, st_id)
            except Exception:
                logger.exception("[%s] FAILED to insert sub_task[%d]: %s",
                                 self.todo_id, i, st.get("title"))

            # For review_loop sub-tasks, set review_chain_id to themselves (chain root)
            if st.get("review_loop"):
                await self.db.execute(
                    "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                    row["id"],
                )

        # Set up dependencies
        for i, st in enumerate(plan.get("sub_tasks", [])):
            depends_on = st.get("depends_on", [])
            if depends_on:
                dep_ids = [sub_task_ids[j] for j in depends_on if j < len(sub_task_ids)]
                if dep_ids:
                    await self.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        sub_task_ids[i],
                        dep_ids,
                    )

        logger.info("[%s] Created %d sub-tasks (ids=%s), transitioning to in_progress/executing",
                    self.todo_id, len(sub_task_ids), sub_task_ids)
        result = await transition_todo(self.db, self.todo_id, "in_progress", sub_state="executing")
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
            await transition_todo(self.db, self.todo_id, "review")
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
        for st in sub_tasks:
            if st["status"] != "pending":
                logger.info("[%s] Sub-task %s status=%s, skipping", self.todo_id, st["id"], st["status"])
                continue
            deps = st["depends_on"] or []
            if deps:
                dep_statuses = await self.db.fetch(
                    "SELECT id, status FROM sub_tasks WHERE id = ANY($1)",
                    deps,
                )
                unmet = [d for d in dep_statuses if d["status"] != "completed"]
                if unmet:
                    logger.info("[%s] Sub-task %s blocked on %d unmet deps", self.todo_id, st["id"], len(unmet))
                    continue
            runnable.append(dict(st))

        logger.info("[%s] %d runnable sub-tasks: %s", self.todo_id, len(runnable),
                    [(r["id"], r["agent_role"], r["title"]) for r in runnable])

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
                    await transition_todo(
                        self.db, self.todo_id, "failed",
                        error_message=f"Sub-tasks failed: {err_detail}",
                    )
                else:
                    # All done — run review
                    await self._phase_review(todo, provider)
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
                await transition_subtask(
                    self.db,
                    str(st["id"]),
                    "failed",
                    error_message=str(result),
                )
            else:
                # Handle review loop: create follow-up sub-tasks
                st_ws = workspace_map.get(str(st["id"]), workspace_path)
                await self._handle_subtask_completion(st, provider, st_ws)

        # Check user messages for steering
        user_msg = await self._check_for_user_messages()
        if user_msg:
            await self._handle_user_message(user_msg, provider)

        # Check if all done now
        all_done, any_failed = await check_all_subtasks_done(self.db, self.todo_id)
        if all_done and not any_failed:
            await self._phase_review(todo, provider)
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
                await transition_todo(self.db, self.todo_id, "failed", error_message=err)

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
        await transition_subtask(self.db, st_id, "assigned")
        await transition_subtask(self.db, st_id, "running")

        await self.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            sub_task["agent_role"],
        )

        iteration_log: list[dict] = []
        unstuck_advice: str | None = None
        role = sub_task["agent_role"]
        role_rules = self._filter_rules_for_role(work_rules or {}, role)
        has_quality_rules = bool(role_rules.get("quality"))

        # Resolve custom agent config for this role
        todo_for_agent = await self._load_todo()
        agent_config = await self._resolve_agent_config(role, str(todo_for_agent["creator_id"]))
        model_override = agent_config.get("model_preference") if agent_config else None

        for iteration in range(1, max_iterations + 1):
            start_time = time.monotonic()

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

                iter_content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_arg,
                    tool_executor=lambda name, args: self.mcp_executor.execute_tool(name, args, mcp_tools),
                    max_rounds=10,
                    on_activity=lambda msg: self._report_activity(st_id, msg),
                    **send_kwargs,
                )

                duration_ms = int((time.monotonic() - start_time) * 1000)
                total_tokens = response.tokens_input + response.tokens_output

                # Update agent run
                await self.db.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'completed', output_result = $2::jsonb,
                        tokens_input = $3, tokens_output = $4,
                        duration_ms = $5, cost_usd = $6, completed_at = NOW()
                    WHERE id = $1
                    """,
                    run["id"],
                    json.dumps({"content": response.content}),
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

                # 4. Record iteration
                action = "implement" if iteration == 1 else ("fix_with_advice" if unstuck_advice else "fix")
                entry = {
                    "iteration": iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "outcome": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                    "error_output": qc_result.get("error_output") if not qc_result["passed"] else None,
                    "learnings": qc_result.get("learnings", []),
                    "files_changed": [],
                    "stuck_check": None,
                    "tokens_used": total_tokens,
                }
                iteration_log.append(entry)

                # Persist iteration log
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2::jsonb WHERE id = $1",
                    sub_task["id"],
                    json.dumps(iteration_log),
                )

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

                    await transition_subtask(
                        self.db, st_id, "completed",
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

                # Clear advice after use
                unstuck_advice = None

                # 6. Stuck detection every 15 iterations
                if iteration % 15 == 0 and iteration < max_iterations:
                    stuck_result = await self._check_if_stuck(iteration_log, provider)
                    entry["stuck_check"] = stuck_result
                    # Update the entry in log
                    iteration_log[-1] = entry
                    await self.db.execute(
                        "UPDATE sub_tasks SET iteration_log = $2::jsonb WHERE id = $1",
                        sub_task["id"],
                        json.dumps(iteration_log),
                    )

                    if stuck_result.get("stuck"):
                        unstuck_advice = stuck_result.get("advice")
                        await self._post_system_message(
                            f"**Stuck detection (iteration {iteration}):** {stuck_result.get('pattern', 'Loop detected')}\n\n"
                            f"Injecting supervisor advice for next iteration."
                        )

            except Exception as e:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                await self.db.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', error_type = 'transient',
                        error_detail = $2, duration_ms = $3, completed_at = NOW()
                    WHERE id = $1
                    """,
                    run["id"],
                    str(e),
                    duration_ms,
                )
                # Record failure in iteration log
                iteration_log.append({
                    "iteration": iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "error",
                    "outcome": f"exception: {str(e)[:200]}",
                    "learnings": [],
                    "tokens_used": 0,
                })
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2::jsonb WHERE id = $1",
                    sub_task["id"],
                    json.dumps(iteration_log),
                )
                raise

        # 7. Hard cutoff — max_iterations reached
        await self._post_system_message(
            f"**Sub-task failed after {max_iterations} iterations:** {sub_task['title']}"
        )
        await self._append_progress_log(
            sub_task, max_iterations, "failed_max_iterations", iteration_log,
        )
        await transition_subtask(
            self.db, st_id, "failed",
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

        # Workspace context
        workspace_context = ""
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=4)
            workspace_context = (
                f"\n\nYou are working inside the project repository root directory.\n"
                f"Project file structure:\n{file_tree}\n"
            )
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

        # Inject tool descriptions for models that need explicit instructions
        if workspace_path:
            system += build_tools_prompt_block(role)

        from agents.orchestrator.output_validator import build_structured_output_instruction
        system += build_structured_output_instruction(role)

        # Iteration learnings (last 5 entries condensed)
        if iteration_log:
            recent = iteration_log[-5:]
            learnings_block = "\n\n## Previous Iteration Learnings\n"
            for entry in recent:
                status = "PASSED" if entry["outcome"] == "passed" else f"FAILED ({entry['outcome']})"
                learnings_block += f"- Iteration {entry['iteration']}: {status}"
                if entry.get("learnings"):
                    learnings_block += " — " + "; ".join(entry["learnings"])
                learnings_block += "\n"
                if entry.get("error_output"):
                    # Truncate error output
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
            SET progress_log = COALESCE(progress_log, '[]'::jsonb) || $2::jsonb,
                updated_at = NOW()
            WHERE id = $1
            """,
            self.todo_id,
            json.dumps([record]),
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

        # Only process review loop sub-tasks
        has_loop = st.get("review_loop") or st.get("review_chain_id")
        if not has_loop:
            return

        chain_id = st.get("review_chain_id") or st["id"]
        role = st["agent_role"]

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
            await self._create_reviewer_subtask(st, chain_id)

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
                # Deterministically commit, push, and create PR — no LLM needed.
                # Find the latest coder sub-task in this chain for commit context.
                coder_st = await self.db.fetchrow(
                    "SELECT * FROM sub_tasks WHERE review_chain_id = $1 "
                    "AND agent_role = 'coder' ORDER BY created_at DESC LIMIT 1",
                    chain_id,
                )
                commit_st = coder_st or st
                pr_info = await self._finalize_subtask_workspace(commit_st, workspace_path)
                if pr_info:
                    pr_url = pr_info.get("url", "N/A")
                    await self._post_system_message(
                        f"**Review approved.** Committed, pushed, and created PR: {pr_url}"
                    )
                    await self._create_merge_subtask(st, chain_id)
                else:
                    await self._post_system_message(
                        "**Review approved.** No changes to commit or PR creation failed."
                    )
            else:
                # needs_changes → create a new coder fix sub-task
                reviewer_feedback = (
                    (st.get("output_result") or {}).get("content", "")
                    if isinstance(st.get("output_result"), dict)
                    else str(st.get("output_result") or "")
                )
                await self._create_fix_subtask(st, chain_id, reviewer_feedback)

    async def _create_reviewer_subtask(self, coder_st: dict, chain_id) -> None:
        """Create a reviewer sub-task that depends on the completed coder sub-task."""
        target_repo_json = coder_st.get("target_repo")
        if target_repo_json and not isinstance(target_repo_json, str):
            target_repo_json = json.dumps(target_repo_json)
        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'reviewer', $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            self.todo_id,
            f"Review: {coder_st['title']}",
            f"Review the changes from sub-task '{coder_st['title']}'. "
            "Check for bugs, security issues, code quality, and adherence to requirements.\n\n"
            "You MUST output a JSON verdict at the end of your response:\n"
            '{"verdict": "approved"} or {"verdict": "needs_changes", "issues": ["issue1", ...]}',
            (coder_st.get("execution_order") or 0) + 1,
            [str(coder_st["id"])],
            chain_id,
            target_repo_json,
        )
        logger.info("Created reviewer sub-task %s for chain %s", row["id"], chain_id)
        await self._post_system_message(
            f"**Review loop:** Created reviewer sub-task for '{coder_st['title']}'"
        )

    async def _create_fix_subtask(
        self, reviewer_st: dict, chain_id, feedback: str,
    ) -> None:
        """Create a coder fix sub-task with reviewer feedback."""
        target_repo_json = reviewer_st.get("target_repo")
        if target_repo_json and not isinstance(target_repo_json, str):
            target_repo_json = json.dumps(target_repo_json)
        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'coder', $4, $5, TRUE, $6, $7::jsonb)
            RETURNING id
            """,
            self.todo_id,
            f"Fix: {reviewer_st['title'].removeprefix('Review: ')}",
            f"Address the reviewer's feedback and fix the issues:\n\n{feedback[:2000]}",
            (reviewer_st.get("execution_order") or 0) + 1,
            [str(reviewer_st["id"])],
            chain_id,
            target_repo_json,
        )
        logger.info("Created fix sub-task %s for chain %s", row["id"], chain_id)
        await self._post_system_message(
            "**Review loop:** Reviewer requested changes. Created fix sub-task."
        )

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
        if target_repo_json and not isinstance(target_repo_json, str):
            target_repo_json = json.dumps(target_repo_json)
        row = await self.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'merge_agent', $4, $5, $6, $7::jsonb)
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

    # ---- MERGE AGENT ----

    async def _execute_merge_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural merge agent: check CI, merge PR, run post-merge builds.

        This is mostly procedural (no LLM needed for happy path).
        """
        st_id = str(sub_task["id"])
        await transition_subtask(self.db, st_id, "assigned")
        await transition_subtask(self.db, st_id, "running")

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
                await transition_subtask(
                    self.db, st_id, "completed",
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
                await transition_subtask(
                    self.db, st_id, "completed",
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
                await transition_subtask(
                    self.db, st_id, "pending",
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
                await transition_subtask(
                    self.db, st_id, "failed",
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
                await transition_subtask(
                    self.db, st_id, "pending",
                    progress_message="Waiting for dependency PRs",
                )
                return

            # 4. Merge the PR
            await self._report_progress(st_id, 70, f"Merging PR #{pr_number}")
            project_settings = project.get("settings_json") or {}
            if isinstance(project_settings, str):
                project_settings = json.loads(project_settings)
            merge_method = project_settings.get("merge_method", "squash")

            merge_result = await git.merge_pull_request(
                owner, repo, pr_number, method=merge_method,
            )

            if not merge_result.get("merged"):
                await self._post_system_message(
                    f"**Merge agent:** Failed to merge PR #{pr_number}: {merge_result.get('message', 'unknown error')}"
                )
                await transition_subtask(
                    self.db, st_id, "failed",
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
            await transition_subtask(
                self.db, st_id, "completed",
                progress_pct=100, progress_message="Merged",
            )

        except Exception as e:
            logger.error("Merge agent failed: %s", e, exc_info=True)
            await transition_subtask(
                self.db, st_id, "failed",
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
        await transition_subtask(self.db, st_id, "assigned")
        await transition_subtask(self.db, st_id, "running")

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

            content, response = await run_tool_loop(
                provider, messages,
                tools=tools_arg,
                tool_executor=lambda name, args: self.mcp_executor.execute_tool(name, args, mcp_tools),
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
                SET status = 'completed', output_result = $2::jsonb,
                    tokens_input = $3, tokens_output = $4,
                    duration_ms = $5, cost_usd = $6, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                json.dumps(validated_output),
                response.tokens_input,
                response.tokens_output,
                duration_ms,
                response.cost_usd,
            )

            # Update sub-task
            await transition_subtask(
                self.db,
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
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self.db.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error_type = 'transient',
                    error_detail = $2, duration_ms = $3, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                str(e),
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
            await transition_todo(self.db, self.todo_id, "review")
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
            await transition_todo(
                self.db, self.todo_id, "completed",
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
            await transition_todo(
                self.db, self.todo_id, "completed",
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
        """Set up a separate workspace for a sub-task targeting a dependency repo."""
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

        # Include stored project understanding from analysis
        if project and project.get("settings_json"):
            settings = project["settings_json"]
            if isinstance(settings, str):
                settings = json.loads(settings)
            understanding = settings.get("project_understanding")
            if understanding:
                context["project_understanding"] = understanding

        return context

    async def _load_chat_history(self) -> list[dict]:
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
        await self.db.execute(
            "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'system', $2)",
            self.todo_id,
            content,
        )
        await self.redis.publish(
            f"task:{self.todo_id}:events",
            json.dumps({"type": "chat_message", "message": {"role": "system", "content": content}}),
        )

    async def _post_assistant_message(self, content: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, 'assistant', $2)",
            self.todo_id,
            content,
        )
        await self.redis.publish(
            f"task:{self.todo_id}:events",
            json.dumps({
                "type": "chat_message",
                "message": {"role": "assistant", "content": content},
            }),
        )

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
        """Publish a granular activity event for live UI display."""
        # Persist latest activity as progress_message so it shows on refresh
        await self.db.execute(
            "UPDATE sub_tasks SET progress_message = $2 WHERE id = $1",
            subtask_id, activity,
        )
        await self.redis.publish(
            f"task:{self.todo_id}:progress",
            json.dumps({
                "type": "activity",
                "sub_task_id": subtask_id,
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
        if role in ("coder", "pr_creator", "report_writer"):
            d_type = {
                "coder": "code_diff",
                "pr_creator": "pull_request",
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
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                self.todo_id,
                run["id"],
                sub_task["id"],
                d_type,
                f"{d_type}: {sub_task['title']}",
                response.content,
                json.dumps(diff_json) if diff_json else None,
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

        # Build workspace context if available
        workspace_context = ""
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=4)
            workspace_context = (
                f"\n\nYou are working inside the project repository root directory.\n"
                f"Project file structure:\n{file_tree}\n"
            )

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

