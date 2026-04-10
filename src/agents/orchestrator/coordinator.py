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

from agents.orchestrator.agent_executor import AgentExecutor
from agents.orchestrator.context_builder import ContextBuilder
from agents.orchestrator.dispatcher import SubtaskDispatcher
from agents.orchestrator.phases import (
    IntakePhase,
    PlanningPhase,
    ExecutionPhase,
    TestingPhase,
    ReviewPhase,
    SubtaskLifecycle,
)

logger = logging.getLogger(__name__)

from agents.agents.registry import (
    build_tools_prompt_block,
    get_agent_definition,
    get_builtin_tool_schemas,
    get_default_system_prompt,
)


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

        # Extracted modules
        self._ctx = ContextBuilder(db, todo_id, self.workspace_mgr, provider_registry)
        self._executor = AgentExecutor(self)
        # Dispatcher is initialised lazily in execution phase (needs bound methods)
        self._dispatcher: SubtaskDispatcher | None = None

        # Phase modules
        self._lifecycle = SubtaskLifecycle(self)
        self._intake = IntakePhase(self)
        self._planning = PlanningPhase(self)
        self._execution = ExecutionPhase(self)
        self._testing = TestingPhase(self)
        self._review = ReviewPhase(self)

    async def _transition_todo(self, target_state: str, **kwargs) -> dict | None:
        """Wrapper that auto-passes db, todo_id, and redis for WS publishing."""
        result = await _transition_todo(
            self.db, self.todo_id, target_state, redis=self.redis, **kwargs,
        )
        if result is None:
            logger.warning(
                "[%s] transition_todo to '%s' failed (state changed concurrently)",
                self.todo_id, target_state,
            )
        return result

    async def _transition_subtask(self, subtask_id: str, target_status: str, **kwargs) -> dict | None:
        """Wrapper that auto-passes db and redis for WS publishing."""
        result = await _transition_subtask(
            self.db, subtask_id, target_status, redis=self.redis, **kwargs,
        )
        if result is None:
            logger.warning(
                "[%s] transition_subtask %s to '%s' failed (status changed concurrently)",
                self.todo_id, subtask_id[:8], target_status,
            )
        return result

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

    async def _resolve_git_provider(self, project: dict) -> tuple:
        """Resolve git provider, owner, and repo from a project row.

        Returns (git_provider_instance, owner, repo).
        Raises ValueError if project has no repo_url.
        """
        from agents.orchestrator.git_providers.factory import (
            create_git_provider,
            parse_repo_url,
        )
        from agents.infra.crypto import decrypt

        repo_url = project["repo_url"]
        if not repo_url:
            raise ValueError("No repo_url configured on project")

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
        return git, owner, repo

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
                logger.info("[%s] → entering intake phase", self.todo_id)
                await self._intake.run(todo, provider)
            case "planning":
                logger.info("[%s] → entering planning phase", self.todo_id)
                await self._planning.run(todo, provider)
            case "plan_ready":
                # Check if human approval is required
                project = await self.db.fetchrow(
                    "SELECT settings_json FROM projects WHERE id = $1",
                    todo["project_id"],
                )
                from agents.utils.settings_helpers import parse_settings, read_setting
                pr_settings = parse_settings((project or {}).get("settings_json"))
                require_plan = read_setting(pr_settings, "planning.require_approval", "require_plan_approval", False)

                plan = todo.get("plan_json")
                logger.info("[%s] → plan_ready: plan exists=%s require_plan_approval=%s",
                            self.todo_id, plan is not None, require_plan)

                if require_plan:
                    # Wait for human — don't auto-approve
                    logger.info("[%s] plan_ready: require_plan_approval=True, waiting for human",
                                self.todo_id)
                    return

                if plan:
                    if isinstance(plan, str):
                        plan = json.loads(plan)
                    logger.info("[%s] → entering auto_approve_plan (sub_tasks=%d)",
                                self.todo_id, len(plan.get("sub_tasks", [])))
                    await self._planning.auto_approve_plan(todo, plan)
                else:
                    logger.warning("[%s] plan_ready but no plan_json! Transitioning back to planning",
                                   self.todo_id)
                    await self._transition_todo("planning", sub_state="re_planning_no_plan")
            case "in_progress":
                logger.info("[%s] → entering execution phase", self.todo_id)
                await self._execution.run(todo, provider)
            case "testing":
                logger.info("[%s] → entering testing phase", self.todo_id)
                await self._testing.run(todo, provider)
            case _:
                logger.warning("[%s] coordinator.run() unhandled state: %s", self.todo_id, todo["state"])


    # ---- INDEX SYNC ----

    async def _sync_task_index_to_project(self, workspace_path: str) -> None:
        """Sync task-level code indexes back to the project-level base."""
        try:
            from agents.indexing import sync_task_index_to_project

            task_index_dir = os.path.join(workspace_path, ".agent_index")
            project_index_dir = os.path.normpath(
                os.path.join(workspace_path, "..", "..", ".agent_index")
            )
            await asyncio.to_thread(
                sync_task_index_to_project, task_index_dir, project_index_dir,
            )
        except Exception:
            logger.debug("[%s] Index sync to project failed (non-fatal)", self.todo_id[:8], exc_info=True)

    # ---- RALPH ITERATION LOOP (delegated to AgentExecutor) ----

    async def _execute_subtask_with_iterations(
        self,
        sub_task: dict,
        provider: AIProvider,
        *,
        workspace_path: str | None = None,
        work_rules: dict | None = None,
        max_iterations: int = 500,
    ) -> None:
        """Delegate to AgentExecutor.execute_iterative."""
        await self._executor.execute_iterative(
            sub_task, provider,
            workspace_path=workspace_path,
            work_rules=work_rules,
            max_iterations=max_iterations,
        )

    async def _run_quality_checks(
        self, workspace_path: str, work_rules: dict, agent_role: str,
        *, submit_data: dict | None = None,
    ) -> dict:
        """Run quality check commands in the workspace.

        Returns {"passed": bool, "reason": str, "error_output": str | None, "learnings": list}.
        """
        if agent_role == "debugger":
            return self._validate_debugger_output(submit_data)
        if agent_role not in ("coder", "tester"):
            return {"passed": True, "reason": "not applicable", "learnings": []}

        quality_rules = work_rules.get("quality", [])
        if not quality_rules:
            return {"passed": True, "reason": "no quality rules", "learnings": []}

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

    @staticmethod
    def _validate_debugger_output(submit_data: dict | None) -> dict:
        """Validate that a debugger produced substantive findings."""
        if not submit_data:
            return {"passed": True, "reason": "no structured output", "learnings": []}

        root_cause = (submit_data.get("root_cause") or "").strip()
        evidence = submit_data.get("evidence") or []

        if len(root_cause) < 20:
            return {
                "passed": False,
                "reason": "Root cause too vague — investigate further with specific file paths and evidence.",
                "error_output": "Root cause must describe the specific code/config issue.",
                "learnings": ["Provide a detailed root cause referencing file paths and line numbers"],
            }

        if not evidence:
            return {
                "passed": False,
                "reason": "No evidence collected. Use tools to gather log lines, code paths, or query results.",
                "error_output": "Evidence list is empty.",
                "learnings": ["Collect at least one piece of evidence before concluding"],
            }

        return {"passed": True, "reason": "debugger output valid", "learnings": []}

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

    async def _persist_execution_events(self, subtask_id: str, events: list[dict]) -> None:
        """Save accumulated execution events to the sub_tasks table."""
        if not events:
            return
        try:
            await self.db.execute(
                "UPDATE sub_tasks SET execution_events = $2 WHERE id = $1",
                subtask_id, events,
            )
        except Exception:
            logger.debug("[%s] Failed to persist execution events for %s", self.todo_id[:8], subtask_id[:8], exc_info=True)

    # ---- DYNAMIC SUBTASK CREATION (builtin tool handler) ----

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
        short_id = str(self.todo_id)[:8]

        # Import git utilities up front (used by both reuse and fresh-clone paths)
        from agents.utils.git_utils import (
            ensure_authenticated_remote,
            resolve_git_credentials,
            run_git_command,
        )
        from agents.orchestrator.git_providers.factory import build_clone_url

        if os.path.isdir(dep_repo_dir):
            # Re-authenticate and verify branch on reused workspace
            await ensure_authenticated_remote(dep_repo_dir, self.db)
            await run_git_command("fetch", "origin", cwd=dep_repo_dir)
            task_branch = f"task/{short_id}-{dep_name}"
            rc, current = await run_git_command(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=dep_repo_dir,
            )
            current = current.strip() if rc == 0 else ""
            if current != task_branch:
                rc, _ = await run_git_command("checkout", task_branch, cwd=dep_repo_dir)
                if rc != 0:
                    await run_git_command("checkout", "-b", task_branch, cwd=dep_repo_dir)
            # Unshallow if needed
            rc_s, is_shallow = await run_git_command(
                "rev-parse", "--is-shallow-repository", cwd=dep_repo_dir,
            )
            if rc_s == 0 and is_shallow.strip() == "true":
                await run_git_command("fetch", "--unshallow", "origin", cwd=dep_repo_dir)
            return dep_task_dir

        os.makedirs(dep_task_dir, exist_ok=True)

        git_provider_id = target_repo.get("git_provider_id")
        if git_provider_id:
            git_provider_id = str(git_provider_id)
        token, provider_type, _ = await resolve_git_credentials(
            self.db, git_provider_id, target_repo["repo_url"],
        )

        clone_url = build_clone_url(target_repo["repo_url"], token, provider_type)
        branch = target_repo.get("default_branch") or "main"

        rc, out = await run_git_command(
            "clone", "--branch", branch, clone_url, dep_repo_dir,
            cwd=settings.workspace_root,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone dependency repo: {out}")

        # Create task branch
        task_branch = f"task/{short_id}-{dep_name}"
        await run_git_command(
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

        # Copy .context/ docs so dep agents can self-serve project context
        project_context = os.path.join(project_dir, ".context")
        dep_context = os.path.join(dep_task_dir, ".context")
        if os.path.isdir(project_context) and not os.path.exists(dep_context):
            try:
                import shutil
                shutil.copytree(project_context, dep_context)
            except Exception:
                logger.debug("Could not copy .context/ into dep workspace")

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
            from agents.utils.settings_helpers import parse_settings, read_setting as _rs
            settings = parse_settings(project["settings_json"])
            understanding = _rs(settings, "understanding.project", "project_understanding")
            if understanding:
                context["project_understanding"] = understanding
            dep_understandings = _rs(settings, "understanding.dependencies", "dep_understandings")
            if dep_understandings:
                context["dep_understandings"] = dep_understandings
            linking_doc = _rs(settings, "understanding.linking", "linking_document")
            if linking_doc:
                context["linking_document"] = linking_doc
            debug_ctx = _rs(settings, "debugging", "debug_context")
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

    async def _post_system_message(self, content: str, metadata: dict | None = None) -> None:
        await self._post_chat_message("system", content, metadata=metadata)

    async def _post_assistant_message(self, content: str, metadata: dict | None = None) -> None:
        await self._post_chat_message("assistant", content, metadata=metadata)

    async def _post_chat_message(self, role: str, content: str, *, metadata: dict | None = None) -> None:
        """Write a chat message to the correct table and publish to relevant channels."""
        session_id = getattr(self, "_chat_session_id", None)

        if session_id:
            # Linked session: write to project_chat_messages
            if metadata:
                await self.db.execute(
                    "INSERT INTO project_chat_messages (project_id, user_id, role, content, metadata_json, session_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    self._chat_project_id, self._chat_user_id, role, content,
                    metadata, session_id,
                )
            else:
                await self.db.execute(
                    "INSERT INTO project_chat_messages (project_id, user_id, role, content, session_id) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    self._chat_project_id, self._chat_user_id, role, content, session_id,
                )
        else:
            # Standard: write to chat_messages
            if metadata:
                await self.db.execute(
                    "INSERT INTO chat_messages (todo_id, role, content, metadata_json) "
                    "VALUES ($1, $2, $3, $4)",
                    self.todo_id, role, content, metadata,
                )
            else:
                await self.db.execute(
                    "INSERT INTO chat_messages (todo_id, role, content) VALUES ($1, $2, $3)",
                    self.todo_id, role, content,
                )

        # Always publish to the task events channel for the todo detail WS
        msg_payload = {"role": role, "content": content}
        if metadata:
            msg_payload["metadata_json"] = metadata
        event_data = json.dumps({
            "type": "chat_message",
            "message": msg_payload,
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

        event_data = json.dumps({
            "type": "activity",
            "phase": "planning",
            "activity": activity,
        })
        await self.redis.publish(f"task:{self.todo_id}:progress", event_data)
        # Also publish to session channel so chat UI can show planning activity
        session_id = getattr(self, "_chat_session_id", None)
        if session_id:
            await self.redis.publish(f"chat:session:{session_id}:activity", event_data)

    def _build_token_streamer(self, subtask_id: str | None = None):
        """Build a buffered on_token callback for streaming LLM text deltas.

        Publishes batched token chunks to ``task:{todo_id}:progress`` (and
        the linked chat session channel, if any) so the UI can render text
        in real-time.  Batches every 20+ chars to avoid flooding Redis.
        """
        channel = f"task:{self.todo_id}:progress"
        session_id = getattr(self, "_chat_session_id", None)
        session_channel = f"chat:session:{session_id}:activity" if session_id else None
        _buf: list[str] = []
        _buf_len = 0

        async def on_token(delta: str) -> None:
            nonlocal _buf_len
            _buf.append(delta)
            _buf_len += len(delta)
            if _buf_len >= 20:
                text = "".join(_buf)
                _buf.clear()
                _buf_len = 0
                payload = json.dumps({
                    "type": "token",
                    "token": text,
                    **({"sub_task_id": subtask_id} if subtask_id else {}),
                })
                await self.redis.publish(channel, payload)
                if session_channel:
                    await self.redis.publish(session_channel, payload)

        async def flush() -> None:
            nonlocal _buf_len
            if _buf:
                text = "".join(_buf)
                _buf.clear()
                _buf_len = 0
                payload = json.dumps({
                    "type": "token",
                    "token": text,
                    **({"sub_task_id": subtask_id} if subtask_id else {}),
                })
                await self.redis.publish(channel, payload)
                if session_channel:
                    await self.redis.publish(session_channel, payload)

        on_token.flush = flush  # type: ignore[attr-defined]
        return on_token

    @staticmethod
    def _get_builtin_tools(workspace_path: str, role: str = "coder") -> list[dict]:
        """Return role-appropriate built-in workspace tool definitions.

        Delegates to the canonical BUILTIN_TOOLS registry in agents.agents.registry.
        These tools are executed directly by McpToolExecutor._execute_builtin()
        rather than going through an external MCP server.
        """
        return get_builtin_tool_schemas(workspace_path, role)

    def _get_dep_index_dirs(self, workspace_path: str) -> dict[str, str]:
        """Build a mapping of dep_name -> index_dir for dependency repos.

        Scans the .agent_index_deps/ directory in the workspace for
        pre-built dependency indexes.
        """
        deps_index_root = os.path.join(workspace_path, ".agent_index_deps")
        if not os.path.isdir(deps_index_root):
            return {}
        result: dict[str, str] = {}
        try:
            for entry in os.listdir(deps_index_root):
                idx_dir = os.path.join(deps_index_root, entry)
                if os.path.isdir(idx_dir):
                    result[entry] = idx_dir
        except OSError:
            pass
        return result

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
            "diff": diff_output[:100_000],  # cap at 100KB
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

            # Resolve target_repo_name for dependency deliverables
            dep_repo_name = None
            target_repo = sub_task.get("target_repo")
            if target_repo:
                if isinstance(target_repo, str):
                    target_repo = json.loads(target_repo) if target_repo else None
                if isinstance(target_repo, dict) and target_repo.get("name"):
                    dep_repo_name = target_repo["name"]

            await self.db.execute(
                """
                INSERT INTO deliverables (
                    todo_id, agent_run_id, sub_task_id, type, title, content_md, content_json,
                    target_repo_name
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                self.todo_id,
                run["id"],
                sub_task["id"],
                d_type,
                f"{d_type}: {sub_task['title']}",
                response.content,
                diff_json,
                dep_repo_name,
            )

    # ---- CANCELLATION CHECK ----

    async def _is_cancelled(self) -> bool:
        """Check if the todo has been cancelled (e.g., by the user via the API)."""
        state = await self.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", self.todo_id,
        )
        return state == "cancelled"

