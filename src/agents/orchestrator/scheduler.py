"""Task Scheduler — the per-task brain.

Replaces ``AgentCoordinator`` with a cleaner architecture:
- Uses ``RunContext`` instead of back-references
- Uses agent instances with declarative spawning for execution
- Delegates to existing phase classes through adapters (intake, planning,
  testing, review) — these will be gradually inlined

The key innovation is in ``_execute_jobs()``: agents return ``AgentResult``
with ``spawn`` declarations, and the scheduler creates follow-up jobs
automatically. No handler registries needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from agents.orchestrator.agents import get_agent, is_llm_role
from agents.orchestrator.job_runner import run_llm_job, run_procedural_job
from agents.orchestrator.run_context import RunContext
from agents.orchestrator.state_machine import check_all_subtasks_done

if TYPE_CHECKING:
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Per-task scheduler. Replaces AgentCoordinator."""

    def __init__(self, todo_id: str, ctx: RunContext) -> None:
        self.todo_id = todo_id
        self.ctx = ctx
        self._coordinator_adapter = None  # Lazy init

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run one scheduling pass for a task."""
        todo = await self.ctx.load_todo()
        logger.info(
            "[%s] scheduler.run(): state=%s sub_state=%s title=%s",
            self.todo_id, todo["state"], todo.get("sub_state"), todo.get("title"),
        )

        # Resolve linked chat session for message routing
        self.ctx.chat_session_id = str(todo["chat_session_id"]) if todo.get("chat_session_id") else None
        self.ctx.chat_project_id = str(todo["project_id"]) if self.ctx.chat_session_id else None
        self.ctx.chat_user_id = str(todo["creator_id"]) if self.ctx.chat_session_id else None

        match todo["state"]:
            case "intake":
                logger.info("[%s] → entering intake phase", self.todo_id)
                await self._run_intake(todo)
            case "planning":
                logger.info("[%s] → entering planning phase", self.todo_id)
                await self._run_planning(todo)
            case "plan_ready":
                logger.info("[%s] → plan_ready", self.todo_id)
                await self._handle_plan_ready(todo)
            case "in_progress":
                logger.info("[%s] → entering execution", self.todo_id)
                await self._execute_jobs(todo)
            case "testing":
                logger.info("[%s] → entering testing phase", self.todo_id)
                await self._run_testing(todo)
            case _:
                logger.warning("[%s] unhandled state: %s", self.todo_id, todo["state"])

    # ------------------------------------------------------------------
    # Phase delegates (use existing phase logic via adapter)
    # ------------------------------------------------------------------

    def _get_adapter(self):
        """Lazy-init coordinator adapter for phase delegation."""
        if self._coordinator_adapter is None:
            self._coordinator_adapter = _CoordinatorAdapter(self.ctx)
        return self._coordinator_adapter

    async def _run_intake(self, todo: dict) -> None:
        """Delegate to existing IntakePhase."""
        from agents.orchestrator.phases import IntakePhase
        adapter = self._get_adapter()
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)
        intake = IntakePhase(adapter)
        await intake.run(todo, provider)

    async def _run_planning(self, todo: dict) -> None:
        """Delegate to existing PlanningPhase."""
        from agents.orchestrator.phases import PlanningPhase
        adapter = self._get_adapter()
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)
        planning = PlanningPhase(adapter)
        await planning.run(todo, provider)

    async def _handle_plan_ready(self, todo: dict) -> None:
        """Handle plan_ready state: auto-approve or wait for human."""
        project = await self.ctx.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1",
            todo["project_id"],
        )
        pr_settings = project["settings_json"] or {} if project else {}
        if isinstance(pr_settings, str):
            pr_settings = json.loads(pr_settings)
        require_plan = pr_settings.get("require_plan_approval", False)

        plan = todo.get("plan_json")
        logger.info(
            "[%s] plan_ready: plan exists=%s require_plan_approval=%s",
            self.todo_id, plan is not None, require_plan,
        )

        if require_plan:
            logger.info("[%s] plan_ready: require_plan_approval=True, waiting for human", self.todo_id)
            return

        if plan:
            if isinstance(plan, str):
                plan = json.loads(plan)
            from agents.orchestrator.phases import PlanningPhase
            adapter = self._get_adapter()
            planning = PlanningPhase(adapter)
            await planning.auto_approve_plan(todo, plan)
        else:
            logger.warning("[%s] plan_ready but no plan_json!", self.todo_id)
            await self.ctx.transition_todo("planning", sub_state="re_planning_no_plan")

    async def _run_testing(self, todo: dict) -> None:
        """Delegate to existing TestingPhase."""
        from agents.orchestrator.phases import TestingPhase
        adapter = self._get_adapter()
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)
        testing = TestingPhase(adapter)
        await testing.run(todo, provider)

    # ------------------------------------------------------------------
    # Execution loop (the core of the rewrite)
    # ------------------------------------------------------------------

    async def _execute_jobs(self, todo: dict) -> None:
        """Execute sub-tasks using agent dispatch + declarative spawning.

        This replaces ExecutionPhase.run() with a cleaner architecture:
        - Agents are resolved from AGENT_REGISTRY
        - LLM agents use run_llm_job(), procedural use run_procedural_job()
        - AgentResult.spawn creates follow-up jobs automatically
        - No handler registries needed
        """
        sub_tasks = await self.ctx.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
            self.todo_id,
        )
        logger.info("[%s] Found %d sub-tasks in DB", self.todo_id, len(sub_tasks))
        for st in sub_tasks:
            tr = st.get("target_repo")
            tr_label = (tr.get("name") if isinstance(tr, dict) else tr) if tr else "main"
            logger.info(
                "[%s]   st id=%s status=%s role=%s title=%s deps=%s target_repo=%s",
                self.todo_id, st["id"], st["status"], st["agent_role"],
                st["title"], st["depends_on"], tr_label,
            )

        if not sub_tasks:
            logger.warning("[%s] No sub-tasks found! Transitioning to review", self.todo_id)
            await self._enter_review(todo)
            return

        # Reset stale sub-tasks from crashed runs
        stale_statuses = ("assigned", "running")
        stale = [st for st in sub_tasks if st["status"] in stale_statuses]
        if stale:
            logger.warning(
                "[%s] Resetting %d stale sub-tasks → pending",
                self.todo_id, len(stale),
            )
            for st in stale:
                await self.ctx.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', updated_at = NOW() WHERE id = $1",
                    st["id"],
                )
            sub_tasks = await self.ctx.db.fetch(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
                self.todo_id,
            )

        # Set up workspace
        workspace_path = await self._setup_workspace(todo)

        # Resolve work rules
        work_rules = await self._resolve_work_rules(todo)
        max_iterations = todo.get("max_iterations") or 50
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)

        # Main execution loop with re-scan
        for scan_round in range(30):  # safety cap
            if await self.ctx.is_cancelled():
                logger.info("[%s] Task cancelled, aborting execution", self.todo_id)
                return

            runnable = await self._find_runnable_jobs()
            if not runnable:
                all_done, any_failed = await check_all_subtasks_done(self.ctx.db, self.todo_id)
                if all_done:
                    await self._handle_all_done(todo, any_failed, workspace_path, provider)
                return

            logger.info(
                "[%s] Scan round %d: dispatching %d jobs",
                self.todo_id, scan_round, len(runnable),
            )

            # Dispatch all runnable jobs in parallel
            tasks = []
            workspace_map: dict[str, str] = {}
            for st in runnable:
                st_workspace = await self._resolve_job_workspace(st, workspace_path)
                if st_workspace is None:
                    continue  # Failed workspace setup already handled
                workspace_map[str(st["id"])] = st_workspace
                tasks.append(
                    self._dispatch_job(st, provider, st_workspace, work_rules, max_iterations)
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for st, result in zip(runnable, results):
                if isinstance(result, Exception):
                    logger.error("Job %s failed: %s", st["id"], result, exc_info=result)
                    error_msg = f"{type(result).__name__}: {result}"
                    if len(error_msg) > 1000:
                        error_msg = error_msg[:1000] + "..."
                    await self.ctx.transition_subtask(
                        str(st["id"]), "failed", error_message=error_msg,
                    )
                else:
                    # Process spawn declarations
                    await self._process_spawn(st, result)

            # Check for user messages
            user_msg = await self.ctx.check_for_user_messages()
            if user_msg:
                adapter = self._get_adapter()
                await adapter._handle_user_message(user_msg, provider)

    # ------------------------------------------------------------------
    # Job dispatch
    # ------------------------------------------------------------------

    async def _dispatch_job(
        self,
        job: dict,
        provider: AIProvider,
        workspace_path: str | None,
        work_rules: dict,
        max_iterations: int,
    ):
        """Dispatch a single job to its agent."""
        role = job["agent_role"]
        agent = get_agent(role)

        if is_llm_role(role):
            return await run_llm_job(
                agent, job, self.ctx, provider,
                workspace_path=workspace_path,
                work_rules=work_rules,
                max_iterations=max_iterations,
            )
        else:
            return await run_procedural_job(
                agent, job, self.ctx,
                workspace_path=workspace_path,
            )

    # ------------------------------------------------------------------
    # Spawn processing (the key mechanism)
    # ------------------------------------------------------------------

    async def _process_spawn(self, parent_job: dict, result) -> None:
        """Create follow-up jobs from AgentResult.spawn declarations.

        This is the core mechanism that replaces handler registries.
        Agents declare what should happen next, and the scheduler creates
        the jobs with proper dependency wiring.
        """
        from agents.orchestrator.agent_result import AgentResult
        if not isinstance(result, AgentResult) or not result.spawn:
            return

        parent_id = str(parent_job["id"])
        chain_id = parent_job.get("review_chain_id") or parent_id

        sibling_ids: list[str] = []
        deferred_siblings: list[tuple] = []

        max_order = await self.ctx.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            self.todo_id,
        )

        for spec in result.spawn:
            dep_ids: list[str] = []
            if spec.depends_on_parent:
                dep_ids.append(parent_id)

            # Resolve target_repo: inherit from parent if not specified
            target_repo = spec.target_repo or parent_job.get("target_repo")

            if spec.depends_on_siblings:
                deferred_siblings.append((spec, dep_ids, target_repo))
                continue

            job_id = await self._insert_spawned_job(
                spec, dep_ids, chain_id, target_repo, max_order + 1,
            )
            sibling_ids.append(job_id)

        # Now handle jobs that depend on all siblings
        for spec, dep_ids, target_repo in deferred_siblings:
            dep_ids.extend(sibling_ids)
            await self._insert_spawned_job(
                spec, dep_ids, chain_id, target_repo, max_order + 2,
            )

        if result.spawn:
            logger.info(
                "[%s] Spawned %d follow-up jobs from %s (role=%s)",
                self.todo_id, len(result.spawn), parent_id[:8],
                parent_job["agent_role"],
            )

    async def _insert_spawned_job(
        self, spec, dep_ids: list[str], chain_id: str,
        target_repo, execution_order: int,
    ) -> str:
        """Insert a spawned job into sub_tasks and return its ID."""
        from agents.orchestrator.agent_result import JobSpec
        row = await self.ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id,
                target_repo, review_loop
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            self.todo_id,
            spec.title,
            spec.description,
            spec.role,
            execution_order,
            dep_ids,
            chain_id if spec.chain_id is None else spec.chain_id,
            target_repo,
            spec.review_loop,
        )
        return str(row["id"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_runnable_jobs(self) -> list[dict]:
        """Find pending jobs whose dependencies are all completed."""
        sub_tasks = await self.ctx.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
            self.todo_id,
        )
        all_st_ids = {str(s["id"]) for s in sub_tasks}

        # Batch fetch dep statuses
        all_dep_ids: set = set()
        for st in sub_tasks:
            if st["status"] == "pending":
                for dep_id in (st["depends_on"] or []):
                    if str(dep_id) in all_st_ids:
                        all_dep_ids.add(dep_id)

        dep_status_map: dict[str, dict] = {}
        if all_dep_ids:
            dep_rows = await self.ctx.db.fetch(
                "SELECT id, status, title FROM sub_tasks WHERE id = ANY($1)",
                list(all_dep_ids),
            )
            dep_status_map = {str(r["id"]): dict(r) for r in dep_rows}

        runnable = []
        for st in sub_tasks:
            if st["status"] != "pending":
                continue
            deps = st["depends_on"] or []
            if deps:
                st_id_str = str(st["id"])
                # Clean broken deps
                broken = [str(d) for d in deps if str(d) == st_id_str or str(d) not in all_st_ids]
                if broken:
                    valid_deps = [d for d in deps if str(d) != st_id_str and str(d) in all_st_ids]
                    await self.ctx.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        st["id"], valid_deps if valid_deps else [],
                    )
                    deps = valid_deps

                if deps:
                    unmet = [
                        d for d in deps
                        if str(d) in dep_status_map
                        and dep_status_map[str(d)]["status"] != "completed"
                    ]
                    if unmet:
                        continue
            runnable.append(dict(st))

        return runnable

    async def _setup_workspace(self, todo: dict) -> str | None:
        """Set up task workspace if project has a repo."""
        project = await self.ctx.db.fetchrow(
            "SELECT repo_url FROM projects WHERE id = $1", todo["project_id"],
        )
        if not project or not project.get("repo_url"):
            return None

        try:
            workspace_path = await self.ctx.workspace_mgr.setup_task_workspace(self.todo_id)
            logger.info("[%s] Workspace ready at %s", self.todo_id, workspace_path)
            return workspace_path
        except Exception:
            logger.exception("[%s] Workspace setup FAILED", self.todo_id)
            await self.ctx.post_system_message(
                "**Workspace setup failed** — could not clone the repository."
            )
            await self.ctx.transition_todo("failed")
            return None

    async def _resolve_job_workspace(self, job: dict, default_workspace: str | None) -> str | None:
        """Resolve workspace for a job, handling dependency repos."""
        target_repo = job.get("target_repo")
        if not target_repo:
            return default_workspace

        dep_label = target_repo.get("name") if isinstance(target_repo, dict) else target_repo
        try:
            adapter = self._get_adapter()
            ws = await adapter._setup_dependency_workspace(job)
            return ws
        except Exception as e:
            logger.error(
                "[%s] Failed to set up dep workspace for %s: %s",
                self.todo_id, dep_label, e, exc_info=True,
            )
            await self.ctx.db.execute(
                "UPDATE sub_tasks SET status = 'failed', error_message = $2 WHERE id = $1",
                job["id"],
                f"Could not set up dependency workspace for '{dep_label}': {e}",
            )
            await self.ctx.post_system_message(
                f"**Sub-task failed:** Could not clone dependency repo '{dep_label}'."
            )
            return None

    async def _resolve_work_rules(self, todo: dict) -> dict:
        """Merge project-level work rules with task-level overrides."""
        project = await self.ctx.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", todo["project_id"],
        )
        project_settings = project["settings_json"] or {} if project else {}
        if isinstance(project_settings, str):
            project_settings = json.loads(project_settings)
        rules = dict(project_settings.get("work_rules", {}))

        overrides = todo.get("rules_override_json") or {}
        if isinstance(overrides, str):
            overrides = json.loads(overrides)
        for category, values in overrides.items():
            rules[category] = values

        return rules

    async def _handle_all_done(
        self, todo: dict, any_failed: bool,
        workspace_path: str | None, provider: AIProvider,
    ) -> None:
        """Handle completion of all sub-tasks."""
        if any_failed:
            # Check retry budget
            if todo["retry_count"] < todo["max_retries"]:
                await self.ctx.db.execute(
                    "UPDATE todo_items SET retry_count = retry_count + 1 WHERE id = $1",
                    self.todo_id,
                )
                await self.ctx.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', retry_count = retry_count + 1 "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    self.todo_id,
                )
                await self.ctx.post_system_message("**Retrying failed sub-tasks...**")
                return
            else:
                failed_tasks = await self.ctx.db.fetch(
                    "SELECT title, error_message FROM sub_tasks "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    self.todo_id,
                )
                err = "; ".join(f"{f['title']}: {f['error_message']}" for f in failed_tasks)
                await self.ctx.transition_todo("failed", error_message=err)
                return

        # All passed — check guardrails
        adapter = self._get_adapter()
        guardrail_created = await adapter._lifecycle.ensure_coding_guardrails(workspace_path)
        if guardrail_created:
            logger.info("[%s] Guardrail subtasks created, will execute on next scan", self.todo_id)
            return  # Next scan round will pick them up

        # Check code push
        code_push_created = await self._ensure_code_push(workspace_path)
        if code_push_created:
            return  # Next scan round will execute the PR job

        # Sync indexes back to project
        if workspace_path:
            await self._sync_index(workspace_path)

        # Enter testing or review
        await self._enter_testing_or_review(todo, provider)

    async def _ensure_code_push(self, workspace_path: str | None) -> bool:
        """Create pr_creator job if coder work was done but no PR exists."""
        if not workspace_path:
            return False

        existing_pr = await self.ctx.db.fetchrow(
            "SELECT id FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
            self.todo_id,
        )
        if existing_pr:
            return False

        existing_pr_task = await self.ctx.db.fetchrow(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
            "AND status IN ('pending', 'assigned', 'running')",
            self.todo_id,
        )
        if existing_pr_task:
            return False

        coder_subtasks = await self.ctx.db.fetch(
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

        max_order = await self.ctx.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            self.todo_id,
        )

        await self.ctx.post_system_message(
            "**Code push:** Creating PR sub-task for completed code changes..."
        )

        await self.ctx.db.fetchrow(
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

    async def _enter_testing_or_review(self, todo: dict, provider: AIProvider) -> None:
        """Route to testing if code work was done, otherwise review."""
        coder_count = await self.ctx.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
            self.todo_id,
        )
        if coder_count and coder_count > 0:
            logger.info("[%s] Code work detected, entering testing", self.todo_id)
            adapter = self._get_adapter()
            from agents.orchestrator.phases import TestingPhase
            testing = TestingPhase(adapter)
            todo = await self.ctx.load_todo()
            await testing.run(todo, provider)
        else:
            logger.info("[%s] No coder work, skipping to review", self.todo_id)
            await self._enter_review(todo)

    async def _enter_review(self, todo: dict) -> None:
        """Enter the review phase."""
        adapter = self._get_adapter()
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)
        from agents.orchestrator.phases import ReviewPhase
        review = ReviewPhase(adapter)
        todo = await self.ctx.load_todo()
        await review.run(todo, provider)

    async def _sync_index(self, workspace_path: str) -> None:
        """Sync task-level code indexes back to project."""
        try:
            from agents.indexing import sync_task_index_to_project
            task_index_dir = os.path.join(workspace_path, ".agent_index")
            project_index_dir = os.path.normpath(
                os.path.join(workspace_path, "..", "..", ".agent_index"),
            )
            await asyncio.to_thread(
                sync_task_index_to_project, task_index_dir, project_index_dir,
            )
        except Exception:
            logger.debug("[%s] Index sync failed (non-fatal)", self.todo_id[:8], exc_info=True)


class _CoordinatorAdapter:
    """Adapter making RunContext look like AgentCoordinator.

    Implements the subset of coordinator methods that the existing
    phase classes (IntakePhase, PlanningPhase, TestingPhase, ReviewPhase,
    SubtaskLifecycle) actually call.

    This is a transitional layer — as phases are gradually rewritten
    to use RunContext directly, this adapter shrinks and eventually
    gets deleted.
    """

    def __init__(self, run_ctx: RunContext) -> None:
        # Store RunContext separately from the _ctx property (which is ContextBuilder)
        self._run_ctx = run_ctx
        self.todo_id = run_ctx.todo_id
        self.db = run_ctx.db
        self.redis = run_ctx.redis
        self.workspace_mgr = run_ctx.workspace_mgr
        self.mcp_executor = run_ctx.mcp_executor
        self.tools_registry = run_ctx.tools_registry
        self.provider_registry = run_ctx.provider_registry
        self.notifier = run_ctx.notifier
        self._chat_session_id = run_ctx.chat_session_id
        self._chat_project_id = run_ctx.chat_project_id
        self._chat_user_id = run_ctx.chat_user_id

        # Activity throttle state
        self._last_activity_publish: dict[str, float] = run_ctx._last_activity_publish
        self._last_activity_persist: dict[str, float] = run_ctx._last_activity_persist

        # Lazy-init modules
        self._dispatcher = None
        self._ctx_builder = None
        self._executor_inst = None
        self._lifecycle_inst = None
        self._execution_inst = None
        self._testing_inst = None
        self._review_inst = None

    # -- Phase module properties (lazy-init) --
    # Note: _ctx is ContextBuilder (what phases reference as coord._ctx)

    @property
    def _ctx(self):
        """Context builder (phases reference coord._ctx for build_context etc.)."""
        if self._ctx_builder is None:
            from agents.orchestrator.context_builder import ContextBuilder
            self._ctx_builder = ContextBuilder(self.db, self.todo_id, self.workspace_mgr, self.provider_registry)
        return self._ctx_builder

    @property
    def _lifecycle(self):
        if self._lifecycle_inst is None:
            from agents.orchestrator.phases import SubtaskLifecycle
            self._lifecycle_inst = SubtaskLifecycle(self)
        return self._lifecycle_inst

    @property
    def _executor(self):
        if self._executor_inst is None:
            from agents.orchestrator.agent_executor import AgentExecutor
            self._executor_inst = AgentExecutor(self)
        return self._executor_inst

    @property
    def _execution(self):
        if self._execution_inst is None:
            from agents.orchestrator.phases import ExecutionPhase
            self._execution_inst = ExecutionPhase(self)
        return self._execution_inst

    @property
    def _testing(self):
        if self._testing_inst is None:
            from agents.orchestrator.phases import TestingPhase
            self._testing_inst = TestingPhase(self)
        return self._testing_inst

    @property
    def _review(self):
        if self._review_inst is None:
            from agents.orchestrator.phases import ReviewPhase
            self._review_inst = ReviewPhase(self)
        return self._review_inst

    # -- Coordinator methods that phases call --

    async def _transition_todo(self, target_state: str, **kwargs):
        return await self._run_ctx.transition_todo(target_state, **kwargs)

    async def _transition_subtask(self, subtask_id: str, target_status: str, **kwargs):
        return await self._run_ctx.transition_subtask(subtask_id, target_status, **kwargs)

    async def _post_system_message(self, content: str, metadata=None):
        await self._run_ctx.post_system_message(content, metadata=metadata)

    async def _track_tokens(self, response):
        await self._run_ctx.track_tokens(response)

    async def _is_cancelled(self):
        return await self._run_ctx.is_cancelled()

    async def _load_todo(self):
        return await self._run_ctx.load_todo()

    async def _check_for_user_messages(self):
        return await self._run_ctx.check_for_user_messages()

    def _build_token_streamer(self, subtask_id=None):
        return self._run_ctx.build_token_streamer(subtask_id)

    async def _report_progress(self, st_id, pct, msg):
        await self._run_ctx.report_progress(st_id, pct, msg)

    async def _report_activity(self, st_id, activity):
        await self._run_ctx.report_activity(st_id, activity)

    async def _report_planning_activity(self, activity):
        await self._run_ctx.report_planning_activity(activity)

    async def _build_context(self, todo):
        return await self._ctx.build_context(todo)

    def _get_builtin_tools(self, workspace_path, role):
        from agents.agents.registry import get_builtin_tool_schemas
        return get_builtin_tool_schemas(workspace_path, role)

    def _get_dep_index_dirs(self, workspace_path):
        deps_root = os.path.join(workspace_path, ".agent_index_deps")
        if not os.path.isdir(deps_root):
            return {}
        result = {}
        try:
            for entry in os.listdir(deps_root):
                idx = os.path.join(deps_root, entry)
                if os.path.isdir(idx):
                    result[entry] = idx
        except OSError:
            pass
        return result

    async def _resolve_agent_config(self, role, owner_id):
        row = await self.db.fetchrow(
            "SELECT * FROM agent_configs WHERE role = $1 AND owner_id = $2 AND is_active = TRUE "
            "ORDER BY updated_at DESC LIMIT 1",
            role, owner_id,
        )
        return dict(row) if row else None

    async def _setup_dependency_workspace(self, sub_task):
        """Set up workspace for a dependency repo sub-task."""
        from agents.config.settings import settings

        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        if not target_repo or not isinstance(target_repo, dict):
            raise ValueError(f"Invalid target_repo: {target_repo}")

        repo_url = target_repo.get("repo_url")
        dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
        if not repo_url:
            raise ValueError(f"No repo_url in target_repo for '{dep_name}'")

        todo = await self._run_ctx.load_todo()
        project_dir = os.path.join(settings.workspace_root, str(todo["project_id"]))
        dep_task_dir = os.path.join(project_dir, "tasks", self.todo_id, f"dep_{dep_name}")
        dep_repo_dir = os.path.join(dep_task_dir, "repo")

        short_id = self.todo_id[:8]

        if os.path.isdir(dep_repo_dir):
            logger.info("[%s] Reusing existing dep workspace: %s", self.todo_id, dep_task_dir)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "--rebase",
                    cwd=dep_repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
            except Exception:
                pass
        else:
            logger.info("[%s] Cloning dep repo '%s' → %s", self.todo_id, dep_name, dep_task_dir)
            os.makedirs(dep_task_dir, exist_ok=True)

            project = await self.db.fetchrow(
                "SELECT git_provider_id FROM projects WHERE id = $1",
                todo["project_id"],
            )
            clone_url = repo_url
            if project and project.get("git_provider_id"):
                try:
                    from agents.infra.crypto import decrypt
                    gp_row = await self.db.fetchrow(
                        "SELECT token_enc FROM git_provider_configs WHERE id = $1",
                        str(project["git_provider_id"]),
                    )
                    if gp_row and gp_row.get("token_enc"):
                        token = decrypt(gp_row["token_enc"])
                        if "github.com" in repo_url:
                            clone_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
                        elif "gitlab" in repo_url:
                            clone_url = repo_url.replace("https://", f"https://oauth2:{token}@")
                except Exception:
                    logger.warning("[%s] Failed to resolve git credentials for dep '%s'", self.todo_id, dep_name)

            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", clone_url, "repo",
                cwd=dep_task_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git clone failed for '{dep_name}': {stderr.decode(errors='replace')[:500]}"
                )

            branch_name = f"task/{short_id}-{dep_name}"
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", "-b", branch_name,
                cwd=dep_repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        return dep_task_dir

    async def _handle_user_message(self, user_msg: str, provider) -> None:
        """Handle a user message injected during execution."""
        from agents.schemas.agent import LLMMessage

        await self._run_ctx.post_system_message(f"**User message received:** {user_msg}")

        messages = [
            LLMMessage(role="system", content=(
                "The user has sent a message during task execution. "
                "Determine if any changes are needed to the current plan."
            )),
            LLMMessage(role="user", content=user_msg),
        ]
        try:
            response = await provider.send_message(messages, temperature=0.1, max_tokens=1024)
            await self._run_ctx.track_tokens(response)
            await self._run_ctx.post_assistant_message(response.content)
        except Exception:
            logger.warning("[%s] Failed to process user message", self.todo_id, exc_info=True)

    async def _sync_task_index_to_project(self, workspace_path: str) -> None:
        """Sync task-level code indexes back to project."""
        try:
            from agents.indexing import sync_task_index_to_project
            task_index_dir = os.path.join(workspace_path, ".agent_index")
            project_index_dir = os.path.normpath(
                os.path.join(workspace_path, "..", "..", ".agent_index"),
            )
            await asyncio.to_thread(
                sync_task_index_to_project, task_index_dir, project_index_dir,
            )
        except Exception:
            logger.debug("[%s] Index sync failed (non-fatal)", self.todo_id[:8], exc_info=True)
