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
from agents.utils.work_rules import resolve_work_rules

if TYPE_CHECKING:
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Per-task scheduler. Replaces AgentCoordinator."""

    def __init__(self, todo_id: str, ctx: RunContext) -> None:
        self.todo_id = todo_id
        self.ctx = ctx
        self._coordinator_adapter = None  # Lazy init
        self._dep_workspace_cache: dict[str, str] = {}  # dep_label → workspace path
        self._dep_workspace_locks: dict[str, asyncio.Lock] = {}  # dep_label → setup lock

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
        from agents.utils.settings_helpers import parse_settings, read_setting
        project = await self.ctx.load_project(str(todo["project_id"]))
        pr_settings = parse_settings((project or {}).get("settings_json"))
        require_plan = read_setting(pr_settings, "planning.require_approval", "require_plan_approval", False)

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

        # NOTE: Stale subtask recovery is handled by the orchestrator loop's
        # _recover_orphaned_tasks on startup. Do NOT reset running subtasks
        # here — they may be legitimately running from a previous dispatch.

        # Load project once for workspace + work rules (single query)
        project = await self.ctx.load_project(str(todo["project_id"]))
        task_root = await self._setup_workspace(todo, project)
        work_rules = self._resolve_work_rules(todo, project)
        max_iterations = todo.get("max_iterations") or 500
        provider = await self.ctx.provider_registry.resolve_for_todo(self.todo_id)

        # Main execution loop with re-scan
        for scan_round in range(30):  # safety cap
            if await self.ctx.is_cancelled():
                logger.info("[%s] Task cancelled, aborting execution", self.todo_id)
                return

            runnable, all_done, any_failed = await self._find_runnable_jobs()
            if not runnable:
                if all_done:
                    await self._handle_all_done(todo, any_failed, task_root, provider)
                return

            logger.info(
                "[%s] Scan round %d: dispatching %d jobs",
                self.todo_id, scan_round, len(runnable),
            )

            # Dispatch all runnable jobs in parallel
            tasks = []
            dispatched: list[dict] = []  # Only jobs that actually got dispatched
            workspace_map: dict[str, str] = {}
            for st in runnable:
                st_workspace = await self._resolve_job_workspace(st, task_root)
                if st_workspace is None:
                    continue  # Failed workspace setup already handled
                workspace_map[str(st["id"])] = st_workspace
                tasks.append(
                    self._dispatch_job(st, provider, st_workspace, work_rules, max_iterations)
                )
                dispatched.append(st)

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for st, result in zip(dispatched, results):
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

            # After coder subtasks complete: commit work + re-index.
            # Group by workspace_path so multi-repo tasks get separate commits.
            completed_coders = [
                st for st, r in zip(dispatched, results)
                if st["agent_role"] == "coder" and not isinstance(r, Exception)
            ]
            if completed_coders:
                coders_by_ws: dict[str, list[dict]] = {}
                for st in completed_coders:
                    ws = workspace_map.get(str(st["id"]))
                    if ws:
                        coders_by_ws.setdefault(ws, []).append(st)
                for ws, coders in coders_by_ws.items():
                    await self._incremental_commit(ws, coders)

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

    async def _find_runnable_jobs(self) -> tuple[list[dict], bool, bool]:
        """Find pending jobs whose dependencies are all completed.

        Returns (runnable_jobs, all_done, any_failed) to avoid a separate
        check_all_subtasks_done query — the data is already in hand.
        """
        sub_tasks = await self.ctx.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
            self.todo_id,
        )
        if not sub_tasks:
            return [], True, False

        # Build lookup from the already-fetched data (no second query needed)
        all_st_map = {str(s["id"]): s for s in sub_tasks}
        statuses = [s["status"] for s in sub_tasks]
        all_done = all(s in ("completed", "failed", "cancelled") for s in statuses)
        any_failed = any(s == "failed" for s in statuses)

        runnable = []
        for st in sub_tasks:
            if st["status"] != "pending":
                continue
            deps = st["depends_on"] or []
            if deps:
                st_id_str = str(st["id"])
                # Clean broken deps (self-refs or refs to non-existent subtasks)
                broken = [str(d) for d in deps if str(d) == st_id_str or str(d) not in all_st_map]
                if broken:
                    valid_deps = [d for d in deps if str(d) != st_id_str and str(d) in all_st_map]
                    await self.ctx.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        st["id"], valid_deps if valid_deps else [],
                    )
                    deps = valid_deps

                if deps:
                    unmet = [
                        d for d in deps
                        if str(d) in all_st_map
                        and all_st_map[str(d)]["status"] != "completed"
                    ]
                    if unmet:
                        continue
            # pr_creator must wait for ALL subtasks targeting the same repo,
            # not just its declared depends_on. New subtasks can be spawned
            # after the PR task was created (e.g. reviewer spawns fix tasks).
            if st["agent_role"] == "pr_creator":
                pr_repo = self._resolve_repo_name(st)
                blocking = [
                    s for s in sub_tasks
                    if str(s["id"]) != str(st["id"])
                    and s["agent_role"] != "pr_creator"
                    and self._resolve_repo_name(s) == pr_repo
                    and s["status"] in ("pending", "assigned", "running")
                ]
                if blocking:
                    # Update depends_on to include the blockers
                    blocker_ids = [str(b["id"]) for b in blocking]
                    current_deps = set(str(d) for d in (st["depends_on"] or []))
                    new_deps = current_deps | set(blocker_ids)
                    if new_deps != current_deps:
                        await self.ctx.db.execute(
                            "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                            st["id"], list(new_deps),
                        )
                    continue

            runnable.append(dict(st))

        return runnable, all_done, any_failed

    @staticmethod
    def _resolve_repo_name(st) -> str:
        """Extract repo name from a subtask's target_repo field."""
        tr = st.get("target_repo")
        if isinstance(tr, str):
            try:
                tr = json.loads(tr)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(tr, dict) and tr.get("name"):
            return tr["name"]
        if isinstance(tr, str) and tr:
            return tr
        return "main"

    async def _setup_workspace(self, todo: dict, project: dict | None = None) -> str | None:
        """Set up task workspace if project has a repo.

        Returns task_root (tasks/{todo_id}/).
        The main repo workspace is at task_root/repos/main/.
        """
        if project is None:
            project = await self.ctx.load_project(str(todo["project_id"]))
        if not project or not project.get("repo_url"):
            return None

        try:
            task_root = await self.ctx.workspace_mgr.setup_task_workspace(self.todo_id)
            logger.info("[%s] Task root ready at %s", self.todo_id, task_root)
            return task_root
        except Exception:
            logger.exception("[%s] Workspace setup FAILED", self.todo_id)
            await self.ctx.post_system_message(
                "**Workspace setup failed** — could not clone the repository."
            )
            await self.ctx.transition_todo("failed")
            return None

    async def _resolve_job_workspace(self, job: dict, task_root: str | None) -> str | None:
        """Resolve workspace for a job, handling dependency repos.

        Resolution order:
        1. Already persisted on the subtask (survives crash/restart)
        2. In-memory cache (same scheduler run, avoids duplicate git ops)
        3. Fresh setup via workspace_mgr.setup_repo_workspace

        The resolved path is always persisted back to sub_tasks.workspace_path
        so it becomes the durable source of truth.
        """
        from agents.orchestrator.workspace import MAIN_REPO

        # 1. Check if already persisted on the subtask
        stored_path = job.get("workspace_path")
        if stored_path and os.path.isdir(stored_path):
            return stored_path

        target_repo = job.get("target_repo")

        # Determine repo_name and repo_url
        if not target_repo:
            # Main repo
            repo_name = MAIN_REPO
            if task_root:
                ws = os.path.join(task_root, MAIN_REPO)
                if os.path.isdir(ws):
                    await self._persist_subtask_workspace(job["id"], ws)
                    return ws
            return None
        else:
            if isinstance(target_repo, str):
                import json as _json
                target_repo = _json.loads(target_repo) if target_repo else {}
            repo_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
            repo_url = target_repo.get("repo_url")
            default_branch = target_repo.get("default_branch") or "main"
            dep_gp_id = target_repo.get("git_provider_id")

        # 2. Return cached workspace if already set up this run
        if repo_name in self._dep_workspace_cache:
            ws = self._dep_workspace_cache[repo_name]
            await self._persist_subtask_workspace(job["id"], ws)
            return ws

        # Serialize setup per dep to prevent concurrent git operations on same dir
        if repo_name not in self._dep_workspace_locks:
            self._dep_workspace_locks[repo_name] = asyncio.Lock()

        async with self._dep_workspace_locks[repo_name]:
            # Double-check after acquiring lock
            if repo_name in self._dep_workspace_cache:
                ws = self._dep_workspace_cache[repo_name]
                await self._persist_subtask_workspace(job["id"], ws)
                return ws

            try:
                ws = await self.ctx.workspace_mgr.setup_repo_workspace(
                    self.todo_id,
                    repo_name,
                    repo_url,
                    default_branch=default_branch,
                    git_provider_id=dep_gp_id,
                )
                self._dep_workspace_cache[repo_name] = ws
                await self._persist_subtask_workspace(job["id"], ws)
                return ws
            except Exception as e:
                logger.error(
                    "[%s] Failed to set up workspace for %s: %s",
                    self.todo_id, repo_name, e, exc_info=True,
                )
                await self.ctx.db.execute(
                    "UPDATE sub_tasks SET status = 'failed', error_message = $2 WHERE id = $1",
                    job["id"],
                    f"Could not set up workspace for '{repo_name}': {e}",
                )
                await self.ctx.post_system_message(
                    f"**Sub-task failed:** Could not clone repo '{repo_name}'."
                )
                return None

    async def _persist_subtask_workspace(self, subtask_id, workspace_path: str) -> None:
        """Store workspace_path on the subtask for crash-resilient lookup."""
        await self.ctx.db.execute(
            "UPDATE sub_tasks SET workspace_path = $2 WHERE id = $1",
            subtask_id, workspace_path,
        )

    @staticmethod
    def _resolve_work_rules(todo: dict, project: dict | None = None) -> dict:
        """Merge project-level work rules with task-level overrides."""
        return resolve_work_rules(todo, project)

    async def _handle_all_done(
        self, todo: dict, any_failed: bool,
        task_root: str | None, provider: AIProvider,
    ) -> None:
        """Handle completion of all sub-tasks."""
        from agents.orchestrator.workspace import MAIN_REPO

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

        # Main repo workspace for guardrails/code push
        main_workspace = os.path.join(task_root, MAIN_REPO) if task_root else None

        # All passed — check guardrails (adds tester/reviewer if missing)
        adapter = self._get_adapter()
        guardrail_created = await adapter._lifecycle.ensure_coding_guardrails(main_workspace)
        if guardrail_created:
            logger.info("[%s] Guardrail subtasks created, will execute on next scan", self.todo_id)
            return

        # Check if testing/review still needed
        has_pending_testing = await self.ctx.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks WHERE todo_id = $1 "
            "AND agent_role IN ('tester', 'reviewer') AND status = 'pending'",
            self.todo_id,
        )
        if has_pending_testing:
            # Testing/review subtasks exist but haven't run yet
            return  # Next scan will execute them

        # Check if we already went through testing phase
        already_tested = await self.ctx.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks WHERE todo_id = $1 "
            "AND agent_role IN ('tester', 'reviewer') AND status = 'completed'",
            self.todo_id,
        )

        if not already_tested:
            # Enter testing/review first — PRs come after
            if main_workspace:
                await self._sync_index(main_workspace)
            await self._enter_testing_or_review(todo, provider)
            return

        # Testing/review done — create PRs (one per repo)
        prs_created = await self._ensure_code_push_per_repo(task_root)
        if prs_created:
            return  # Next scan will execute the PR jobs

        # Everything done — transition to review/completed
        await self.ctx.transition_todo("review")

    async def _ensure_code_push_per_repo(self, task_root: str | None) -> bool:
        """Create one pr_creator job per repo that had completed coder work.

        Groups completed coder subtasks by their target repo and creates
        a separate PR subtask for each repo. Only creates PRs for repos
        that don't already have a PR deliverable or pending pr_creator task.

        Returns True if any PR subtasks were created.
        """
        if not task_root:
            return False

        # Skip if pr_creator tasks already exist
        existing_pr_task = await self.ctx.db.fetchrow(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
            "AND status IN ('pending', 'assigned', 'running')",
            self.todo_id,
        )
        if existing_pr_task:
            return False

        # Get all completed coder subtasks
        coder_subtasks = await self.ctx.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
            "AND status = 'completed' ORDER BY created_at",
            self.todo_id,
        )
        if not coder_subtasks:
            return False

        # Group by target repo
        by_repo: dict[str, list[dict]] = {}
        for st in coder_subtasks:
            target = st.get("target_repo")
            if isinstance(target, str):
                try:
                    target = json.loads(target)
                except (json.JSONDecodeError, TypeError):
                    target = None
            repo_name = "main"
            if isinstance(target, dict) and target.get("name"):
                repo_name = target["name"]
            elif isinstance(target, str) and target:
                repo_name = target
            by_repo.setdefault(repo_name, []).append(dict(st))

        # Check which repos already have PRs
        existing_prs = await self.ctx.db.fetch(
            "SELECT branch_name FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
            self.todo_id,
        )
        existing_branches = {r["branch_name"] for r in existing_prs if r.get("branch_name")}

        max_order = await self.ctx.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            self.todo_id,
        )

        # Get ALL subtasks to check for pending/running work per repo
        all_subtasks = await self.ctx.db.fetch(
            "SELECT id, status, agent_role, target_repo FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role != 'pr_creator'",
            self.todo_id,
        )

        # Build a map of repo_name → list of incomplete subtask IDs
        def _repo_name_of(st):
            tr = st.get("target_repo")
            if isinstance(tr, str):
                try: tr = json.loads(tr)
                except: pass
            if isinstance(tr, dict) and tr.get("name"):
                return tr["name"]
            if isinstance(tr, str) and tr:
                return tr
            return "main"

        created = 0
        for repo_name, coders in by_repo.items():
            ws = os.path.join(task_root, repo_name)
            if not os.path.isdir(ws):
                continue

            # Collect ALL subtask IDs for this repo that the PR should depend on
            repo_dep_ids = []
            has_incomplete = False
            for st in all_subtasks:
                if _repo_name_of(st) != repo_name:
                    continue
                st_id = str(st["id"])
                if st["status"] in ("pending", "assigned", "running"):
                    has_incomplete = True
                    repo_dep_ids.append(st_id)
                elif st["status"] == "completed":
                    repo_dep_ids.append(st_id)

            if not repo_dep_ids:
                continue

            # Build target_repo JSON for dep repos
            target_repo_json = None
            if repo_name != "main":
                for st in coders:
                    tr = st.get("target_repo")
                    if tr and isinstance(tr, dict):
                        target_repo_json = tr
                        break

            title = f"Create Pull Request — {repo_name}" if repo_name != "main" else "Create Pull Request"

            if has_incomplete:
                logger.info(
                    "[%s] PR for %s: has incomplete subtasks, adding them as dependencies",
                    self.todo_id, repo_name,
                )

            await self.ctx.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, target_repo, workspace_path
                )
                VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6, $7)
                RETURNING id
                """,
                self.todo_id,
                title,
                f"Commit changes, push to feature branch, and create PR for {repo_name} repo.",
                max_order + 1,
                repo_dep_ids,
                target_repo_json,
                ws,
            )
            created += 1

        if created:
            repos_str = ", ".join(by_repo.keys())
            await self.ctx.post_system_message(
                f"**Code push:** Creating {created} PR sub-task(s) for repos: {repos_str}"
            )

        return created > 0

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

    async def _incremental_commit(self, workspace_path: str, completed_coders: list[dict]) -> None:
        """Commit coder subtask work to git immediately after completion.

        workspace_path IS the git working directory (no /repo/ subdir).
        Each batch of completed coders gets one commit with a descriptive message.
        """
        from agents.utils.git_utils import run_git_command

        if not os.path.isdir(os.path.join(workspace_path, ".git")):
            return

        try:
            rc, _ = await run_git_command("add", "-A", cwd=workspace_path)
            if rc != 0:
                return

            rc, _ = await run_git_command("diff", "--cached", "--quiet", cwd=workspace_path)
            if rc == 0:
                return  # nothing staged

            titles = [st["title"] for st in completed_coders]
            if len(titles) == 1:
                msg = f"[agents] {titles[0]}"
            else:
                msg = f"[agents] {len(titles)} subtasks: " + ", ".join(t[:40] for t in titles)

            rc, out = await run_git_command("commit", "-m", msg, "--no-verify", cwd=workspace_path)
            if rc == 0:
                _, commit_hash = await run_git_command("rev-parse", "HEAD", cwd=workspace_path)
                commit_hash = commit_hash.strip()
                for st in completed_coders:
                    await self.ctx.db.execute(
                        "UPDATE sub_tasks SET commit_hash = $2 WHERE id = $1",
                        st["id"], commit_hash,
                    )
                logger.info("[%s] Incremental commit %s: %s", self.todo_id[:8], commit_hash[:10], msg[:80])
            else:
                logger.warning("[%s] Incremental commit failed: %s", self.todo_id[:8], out[:200])
        except Exception:
            logger.debug("[%s] Incremental commit error (non-fatal)", self.todo_id[:8], exc_info=True)



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

    @property
    def _planning(self):
        from agents.orchestrator.phases import PlanningPhase
        return PlanningPhase(self)

    async def _build_context(self, todo):
        return await self._ctx.build_context(todo)

    def _get_builtin_tools(self, workspace_path, role):
        from agents.agents.registry import get_builtin_tool_schemas
        return get_builtin_tool_schemas(workspace_path, role)

    async def _resolve_agent_config(self, role, owner_id):
        row = await self.db.fetchrow(
            "SELECT * FROM agent_configs WHERE role = $1 AND owner_id = $2 AND is_active = TRUE "
            "ORDER BY updated_at DESC LIMIT 1",
            role, owner_id,
        )
        return dict(row) if row else None

    async def _maybe_create_deliverable(self, sub_task, response, run, *, workspace_path=None):
        from agents.orchestrator.job_runner import _maybe_create_deliverable
        await _maybe_create_deliverable(self._run_ctx, sub_task, response, run, workspace_path=workspace_path)

    async def _handle_create_subtask_tool(self, parent_subtask, args, workspace_path):
        from agents.orchestrator.job_runner import _handle_create_subtask_tool
        return await _handle_create_subtask_tool(self._run_ctx, parent_subtask, args, workspace_path)

    async def _run_quality_checks(self, workspace_path, work_rules, role, *, submit_data=None):
        from agents.orchestrator.job_runner import _run_quality_checks
        return await _run_quality_checks(self._run_ctx, workspace_path, work_rules, role, submit_data=submit_data)

    async def _append_progress_log(self, sub_task, iterations_used, outcome, iteration_log):
        from agents.orchestrator.job_runner import _append_progress_log
        await _append_progress_log(self._run_ctx, sub_task, iterations_used, outcome, iteration_log)

    async def _persist_execution_events(self, subtask_id, events):
        from agents.orchestrator.job_runner import _persist_execution_events
        await _persist_execution_events(self._run_ctx, subtask_id, events)

    async def _setup_dependency_workspace(self, sub_task):
        """Set up workspace for a dependency repo sub-task.

        Delegates to WorkspaceManager.setup_repo_workspace for unified handling.
        """
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        if not target_repo or not isinstance(target_repo, dict):
            raise ValueError(f"Invalid target_repo: {target_repo}")

        repo_url = target_repo.get("repo_url")
        dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
        if not repo_url:
            raise ValueError(f"No repo_url in target_repo for '{dep_name}'")

        return await self.workspace_mgr.setup_repo_workspace(
            self.todo_id,
            dep_name,
            repo_url,
            default_branch=target_repo.get("default_branch") or "main",
            git_provider_id=target_repo.get("git_provider_id"),
        )

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

