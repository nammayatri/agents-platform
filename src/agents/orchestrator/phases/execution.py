"""Execution phase — dispatch and run sub-tasks in parallel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from agents.orchestrator.dispatcher import SubtaskDispatcher
from agents.orchestrator.state_machine import check_all_subtasks_done

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)


class ExecutionPhase:
    """Execute sub-tasks in parallel, respecting dependencies."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, todo: dict, provider: AIProvider) -> None:
        """Execute sub-tasks in parallel, respecting dependencies."""
        coord = self._coord
        logger.info("[%s] _phase_execution started (todo state=%s, sub_state=%s)",
                    coord.todo_id, todo.get("state"), todo.get("sub_state"))
        sub_tasks = await coord.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
            coord.todo_id,
        )
        logger.info("[%s] Found %d sub-tasks in DB", coord.todo_id, len(sub_tasks))
        for st in sub_tasks:
            tr = st.get("target_repo")
            tr_label = (tr.get("name") if isinstance(tr, dict) else tr) if tr else "main"
            logger.info("[%s]   sub_task id=%s status=%s role=%s title=%s deps=%s target_repo=%s",
                        coord.todo_id, st["id"], st["status"], st["agent_role"],
                        st["title"], st["depends_on"], tr_label)
        if not sub_tasks:
            # No sub-tasks — skip to review
            logger.warning("[%s] No sub-tasks found in DB! Transitioning to review", coord.todo_id)
            await coord._transition_todo("review")
            return

        # Reset stale sub-tasks from previous crashed coordinator runs.
        stale_statuses = ("assigned", "running")
        stale = [st for st in sub_tasks if st["status"] in stale_statuses]
        if stale:
            logger.warning("[%s] Resetting %d stale sub-tasks (assigned/running → pending): %s",
                           coord.todo_id, len(stale),
                           [(str(s["id"])[:8], s["status"], s["agent_role"]) for s in stale])
            for st in stale:
                await coord.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', updated_at = NOW() WHERE id = $1",
                    st["id"],
                )
            # Re-fetch after reset
            sub_tasks = await coord.db.fetch(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order, created_at",
                coord.todo_id,
            )

        # Set up task workspace if project has a repo
        workspace_path = None
        project = await coord.db.fetchrow(
            "SELECT repo_url FROM projects WHERE id = $1", todo["project_id"]
        )
        if project and project.get("repo_url"):
            logger.info("[%s] Setting up workspace for repo %s", coord.todo_id, project["repo_url"])
            try:
                workspace_path = await coord.workspace_mgr.setup_task_workspace(coord.todo_id)
                logger.info("[%s] Task workspace ready at %s", coord.todo_id, workspace_path)
            except Exception:
                logger.exception("[%s] Workspace setup FAILED for %s — task cannot proceed without workspace",
                                 coord.todo_id, project["repo_url"])
                await coord._post_system_message(
                    "**Workspace setup failed** — could not clone the repository. "
                    "Check that the repo URL and git credentials are correct."
                )
                await coord._transition_todo("failed")
                return
        else:
            logger.info("[%s] No repo_url on project, skipping workspace setup", coord.todo_id)

        # Find runnable sub-tasks (pending + all dependencies completed)
        runnable = []
        all_st_ids = {str(s["id"]) for s in sub_tasks}

        # Batch: collect ALL dep IDs and fetch statuses in one query
        all_dep_ids: set = set()
        for st in sub_tasks:
            if st["status"] == "pending":
                for dep_id in (st["depends_on"] or []):
                    dep_str = str(dep_id)
                    if dep_str in all_st_ids:
                        all_dep_ids.add(dep_id)
        dep_status_map: dict[str, dict] = {}
        if all_dep_ids:
            dep_rows = await coord.db.fetch(
                "SELECT id, status, title FROM sub_tasks WHERE id = ANY($1)",
                list(all_dep_ids),
            )
            dep_status_map = {str(r["id"]): dict(r) for r in dep_rows}

        for st in sub_tasks:
            if st["status"] != "pending":
                logger.info("[%s] Sub-task %s status=%s, skipping", coord.todo_id, st["id"], st["status"])
                continue
            deps = st["depends_on"] or []
            if deps:
                # Detect broken deps: self-references or refs to non-existent subtasks
                st_id_str = str(st["id"])
                broken = [str(d) for d in deps if str(d) == st_id_str or str(d) not in all_st_ids]
                if broken:
                    logger.warning(
                        "[%s] Sub-task %s (%s) has broken depends_on refs: %s — clearing them",
                        coord.todo_id, st["id"], st["title"], broken,
                    )
                    valid_deps = [d for d in deps if str(d) != st_id_str and str(d) in all_st_ids]
                    await coord.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        st["id"],
                        valid_deps if valid_deps else [],
                    )
                    deps = valid_deps

                if deps:
                    unmet = [dep_status_map[str(d)] for d in deps
                             if str(d) in dep_status_map and dep_status_map[str(d)]["status"] != "completed"]
                    if unmet:
                        unmet_detail = [(str(d["id"])[:8], d["status"], d["title"]) for d in unmet]
                        logger.info(
                            "[%s] Sub-task %s (%s) blocked on %d unmet deps: %s",
                            coord.todo_id, st["id"], st["title"], len(unmet), unmet_detail,
                        )
                        continue
            runnable.append(dict(st))

        logger.info("[%s] %d runnable sub-tasks: %s", coord.todo_id, len(runnable),
                    [(r["id"], r["agent_role"], r["title"]) for r in runnable])

        # Check for cancellation before dispatching
        if await coord._is_cancelled():
            logger.info("[%s] Task cancelled before dispatch, aborting execution", coord.todo_id)
            return

        if not runnable:
            # Check if all done
            logger.warning("[%s] No runnable sub-tasks found (total=%d). Checking if all done...",
                           coord.todo_id, len(sub_tasks))
            all_done, any_failed = await check_all_subtasks_done(coord.db, coord.todo_id)
            logger.info("[%s] all_done=%s, any_failed=%s", coord.todo_id, all_done, any_failed)
            if all_done:
                if any_failed:
                    failed_tasks = await coord.db.fetch(
                        "SELECT title, error_message FROM sub_tasks "
                        "WHERE todo_id = $1 AND status = 'failed'",
                        coord.todo_id,
                    )
                    err_detail = "; ".join(
                        f"{f['title']}: {f['error_message'] or 'unknown'}" for f in failed_tasks
                    )
                    await coord._transition_todo(
                        "failed",
                        error_message=f"Sub-tasks failed: {err_detail}",
                    )
                else:
                    # All done — sync indexes back to project, then run testing/review
                    if workspace_path:
                        await coord._sync_task_index_to_project(workspace_path)
                    await self.enter_testing_or_review(todo, provider)
            # else: some tasks still running, will be picked up next cycle
            return

        # Resolve work rules for RALPH loop
        work_rules = await self.resolve_work_rules(todo)
        has_quality_rules = bool(work_rules.get("quality"))
        max_iterations = todo.get("max_iterations") or 50

        # Lazy-init the dispatcher (needs bound methods available after __init__)
        if coord._dispatcher is None:
            coord._dispatcher = SubtaskDispatcher(
                execute_simple=coord._executor.execute_single,
                execute_iterative=coord._executor.execute_iterative,
                role_handlers={
                    "merge_agent": coord._lifecycle.execute_merge_subtask,
                    "merge_observer": coord._lifecycle.execute_merge_observer_subtask,
                    "pr_creator": coord._lifecycle.execute_pr_creator_subtask,
                    "release_build_watcher": coord._lifecycle.execute_build_watcher_subtask,
                    "release_deployer": coord._lifecycle.execute_release_deployer_subtask,
                },
            )

        # Execute runnable sub-tasks in parallel
        logger.info("[%s] Dispatching %d sub-tasks (quality_rules=%s, max_iter=%d, workspace=%s)",
                    coord.todo_id, len(runnable), has_quality_rules, max_iterations, workspace_path)
        tasks = []
        workspace_map: dict[str, str] = {}  # subtask_id → resolved workspace
        for st in runnable:
            # Resolve per-subtask workspace (dep repos get their own)
            st_workspace = workspace_path
            target_repo = st.get("target_repo")
            if target_repo:
                dep_label = target_repo.get("name") if isinstance(target_repo, dict) else target_repo
                logger.info("[%s] Sub-task %s targets dep repo '%s'", coord.todo_id, st["id"], dep_label)
                try:
                    st_workspace = await coord._setup_dependency_workspace(st)
                    logger.info("[%s] Dep workspace for %s: %s", coord.todo_id, st["id"], st_workspace)
                except Exception as e:
                    logger.error(
                        "[%s] Failed to set up dep workspace for %s (repo=%s): %s",
                        coord.todo_id, st["id"], dep_label, e, exc_info=True,
                    )
                    # Fail the subtask instead of silently running against the wrong repo
                    await coord.db.execute(
                        "UPDATE sub_tasks SET status = 'failed', error_message = $2 WHERE id = $1",
                        st["id"],
                        f"Could not set up dependency workspace for '{dep_label}': {e}",
                    )
                    await coord._post_system_message(
                        f"**Sub-task failed:** Could not clone dependency repo '{dep_label}'. "
                        f"Check git credentials and repo URL. Error: {e}"
                    )
                    continue
            workspace_map[str(st["id"])] = st_workspace

            logger.info("[%s] Sub-task %s: dispatching (role=%s)", coord.todo_id, st["id"], st["agent_role"])
            tasks.append(coord._dispatcher.build_coro(
                st, provider, st_workspace,
                use_iterations=has_quality_rules,
                work_rules=work_rules,
                max_iterations=max_iterations,
            ))

        logger.info("[%s] Waiting on %d sub-task coroutines...", coord.todo_id, len(tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[%s] All sub-tasks returned. Processing results...", coord.todo_id)

        for st, result in zip(runnable, results):
            if isinstance(result, Exception):
                logger.error("Sub-task %s failed: %s", st["id"], result, exc_info=result)
                error_msg = f"{type(result).__name__}: {result}"
                if len(error_msg) > 1000:
                    error_msg = error_msg[:1000] + "..."
                await coord._transition_subtask(
                    str(st["id"]),
                    "failed",
                    error_message=error_msg,
                )
            else:
                # Handle review loop: create follow-up sub-tasks
                st_ws = workspace_map.get(str(st["id"]), workspace_path)
                await coord._lifecycle.handle_subtask_completion(st, provider, st_ws)

        # Check user messages for steering
        user_msg = await coord._check_for_user_messages()
        if user_msg:
            await coord._handle_user_message(user_msg, provider)

        # Check for cancellation after batch
        if await coord._is_cancelled():
            logger.info("[%s] Task cancelled after batch, aborting execution", coord.todo_id)
            return

        # Re-scan for newly unblocked subtasks
        for rescan_round in range(1, 20):  # safety cap
            all_done, any_failed = await check_all_subtasks_done(coord.db, coord.todo_id)
            if all_done:
                break

            fresh_sub_tasks = await coord.db.fetch(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 ORDER BY execution_order",
                coord.todo_id,
            )

            # Batch dep status lookup for all pending subtasks
            rescan_dep_ids: set = set()
            for st2 in fresh_sub_tasks:
                if st2["status"] == "pending":
                    for d in (st2["depends_on"] or []):
                        rescan_dep_ids.add(d)
            rescan_dep_map: dict[str, str] = {}
            if rescan_dep_ids:
                _dep_rows = await coord.db.fetch(
                    "SELECT id, status FROM sub_tasks WHERE id = ANY($1)",
                    list(rescan_dep_ids),
                )
                rescan_dep_map = {str(r["id"]): r["status"] for r in _dep_rows}

            next_runnable = []
            for st2 in fresh_sub_tasks:
                if st2["status"] != "pending":
                    continue
                deps2 = st2["depends_on"] or []
                if deps2:
                    if any(rescan_dep_map.get(str(d)) != "completed" for d in deps2):
                        continue
                next_runnable.append(dict(st2))

            if not next_runnable:
                logger.info("[%s] Re-scan round %d: no more runnable subtasks", coord.todo_id, rescan_round)
                break

            if await coord._is_cancelled():
                logger.info("[%s] Task cancelled during re-scan, aborting", coord.todo_id)
                return

            logger.info("[%s] Re-scan round %d: found %d newly runnable subtasks",
                        coord.todo_id, rescan_round, len(next_runnable))
            next_tasks = []
            for st2 in next_runnable:
                st2_ws = workspace_path
                if st2.get("target_repo"):
                    try:
                        st2_ws = await coord._setup_dependency_workspace(st2)
                    except Exception:
                        pass
                workspace_map[str(st2["id"])] = st2_ws

                next_tasks.append(coord._dispatcher.build_coro(
                    st2, provider, st2_ws,
                    use_iterations=has_quality_rules,
                    work_rules=work_rules,
                    max_iterations=max_iterations,
                ))

            rescan_results = await asyncio.gather(*next_tasks, return_exceptions=True)
            for st2, result2 in zip(next_runnable, rescan_results):
                if isinstance(result2, Exception):
                    logger.error("Sub-task %s failed: %s", st2["id"], result2, exc_info=result2)
                    await coord._transition_subtask(str(st2["id"]), "failed", error_message=str(result2))
                else:
                    st2_ws = workspace_map.get(str(st2["id"]), workspace_path)
                    await coord._lifecycle.handle_subtask_completion(st2, provider, st2_ws)

        # Final check
        all_done, any_failed = await check_all_subtasks_done(coord.db, coord.todo_id)
        if all_done and not any_failed:
            # Guardrail: ensure coding tasks have test + review subtasks
            guardrail_created = await coord._lifecycle.ensure_coding_guardrails(workspace_path)
            if guardrail_created:
                logger.info("[%s] Guardrail subtasks created, executing them", coord.todo_id)
                guardrail_tasks = await coord.db.fetch(
                    "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'pending' "
                    "ORDER BY execution_order, created_at",
                    coord.todo_id,
                )
                # Batch dep lookup for guardrail subtasks
                g_dep_ids: set = set()
                for gst in guardrail_tasks:
                    for d in (gst["depends_on"] or []):
                        g_dep_ids.add(d)
                g_dep_map: dict[str, str] = {}
                if g_dep_ids:
                    _g_rows = await coord.db.fetch(
                        "SELECT id, status FROM sub_tasks WHERE id = ANY($1)",
                        list(g_dep_ids),
                    )
                    g_dep_map = {str(r["id"]): r["status"] for r in _g_rows}

                for gst in guardrail_tasks:
                    deps = gst["depends_on"] or []
                    if deps:
                        if any(g_dep_map.get(str(d)) != "completed" for d in deps):
                            continue
                    gst_ws = workspace_path
                    gst_target = gst.get("target_repo")
                    if gst_target:
                        gst_dep_label = gst_target.get("name") if isinstance(gst_target, dict) else gst_target
                        try:
                            gst_ws = await coord._setup_dependency_workspace(gst)
                        except Exception as e:
                            logger.error(
                                "[%s] Failed to set up dep workspace for guardrail %s: %s",
                                coord.todo_id, gst["id"], e, exc_info=True,
                            )
                            await coord.db.execute(
                                "UPDATE sub_tasks SET status = 'failed', error_message = $2 WHERE id = $1",
                                gst["id"],
                                f"Could not set up dependency workspace for '{gst_dep_label}': {e}",
                            )
                            continue
                    await coord._dispatcher.dispatch(
                        dict(gst), provider, gst_ws,
                        use_iterations=has_quality_rules,
                        work_rules=work_rules,
                        max_iterations=max_iterations,
                    )

                # Re-scan: guardrail subtasks may have unblocked others
                for gr_round in range(1, 10):
                    gr_fresh = await coord.db.fetch(
                        "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'pending' "
                        "ORDER BY execution_order, created_at",
                        coord.todo_id,
                    )
                    # Batch dep lookup for guardrail re-scan
                    gr_dep_ids: set = set()
                    for gst2 in gr_fresh:
                        for d in (gst2["depends_on"] or []):
                            gr_dep_ids.add(d)
                    gr_dep_map: dict[str, str] = {}
                    if gr_dep_ids:
                        _gr_rows = await coord.db.fetch(
                            "SELECT id, status FROM sub_tasks WHERE id = ANY($1)",
                            list(gr_dep_ids),
                        )
                        gr_dep_map = {str(r["id"]): r["status"] for r in _gr_rows}

                    gr_runnable = []
                    for gst2 in gr_fresh:
                        deps2 = gst2["depends_on"] or []
                        if deps2:
                            if any(gr_dep_map.get(str(d)) != "completed" for d in deps2):
                                continue
                        gr_runnable.append(dict(gst2))
                    if not gr_runnable:
                        break
                    logger.info("[%s] Guardrail re-scan round %d: %d runnable",
                                coord.todo_id, gr_round, len(gr_runnable))
                    for gst2 in gr_runnable:
                        gst2_ws = workspace_path
                        await coord._dispatcher.dispatch(
                            gst2, provider, gst2_ws,
                            use_iterations=has_quality_rules,
                            work_rules=work_rules,
                            max_iterations=max_iterations,
                        )

                # After guardrail execution, re-check completion
                all_done, any_failed = await check_all_subtasks_done(coord.db, coord.todo_id)
                if all_done and not any_failed:
                    code_push_created = await self.ensure_code_push(workspace_path)
                    if code_push_created:
                        await self._execute_code_push_chain(provider, workspace_path)
                    await coord._sync_task_index_to_project(workspace_path)
                    await self.enter_testing_or_review(todo, provider)
                elif all_done and any_failed:
                    failed_tasks = await coord.db.fetch(
                        "SELECT title, error_message FROM sub_tasks "
                        "WHERE todo_id = $1 AND status = 'failed'",
                        coord.todo_id,
                    )
                    err = "; ".join(f"{f['title']}: {f['error_message']}" for f in failed_tasks)
                    await coord._transition_todo("failed", error_message=err)
            else:
                code_push_created = await self.ensure_code_push(workspace_path)
                if code_push_created:
                    await self._execute_code_push_chain(provider, workspace_path)
                await coord._sync_task_index_to_project(workspace_path)
                await self.enter_testing_or_review(todo, provider)
        elif all_done and any_failed:
            # Check if we should retry
            if todo["retry_count"] < todo["max_retries"]:
                await coord.db.execute(
                    "UPDATE todo_items SET retry_count = retry_count + 1 WHERE id = $1",
                    coord.todo_id,
                )
                # Reset failed sub-tasks to pending
                await coord.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', retry_count = retry_count + 1 "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    coord.todo_id,
                )
                await coord._post_system_message("**Retrying failed sub-tasks...**")
            else:
                failed_tasks = await coord.db.fetch(
                    "SELECT title, error_message FROM sub_tasks "
                    "WHERE todo_id = $1 AND status = 'failed'",
                    coord.todo_id,
                )
                err = "; ".join(f"{f['title']}: {f['error_message']}" for f in failed_tasks)
                await coord._transition_todo("failed", error_message=err)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _execute_code_push_chain(self, provider: AIProvider, workspace_path: str | None) -> None:
        """Execute pr_creator and merge_agent subtasks created by ensure_code_push."""
        coord = self._coord
        pr_st = await coord.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
            "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            coord.todo_id,
        )
        if pr_st:
            await coord._lifecycle.execute_pr_creator_subtask(dict(pr_st), provider, workspace_path)
        # pr_creator may have created a merge_agent or merge_observer — execute it
        merge_st = await coord.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 "
            "AND agent_role IN ('merge_agent', 'merge_observer') "
            "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            coord.todo_id,
        )
        if merge_st:
            merge_st = dict(merge_st)
            if merge_st["agent_role"] == "merge_observer":
                await coord._lifecycle.execute_merge_observer_subtask(merge_st, provider, workspace_path)
            else:
                await coord._lifecycle.execute_merge_subtask(merge_st, provider, workspace_path)

    async def resolve_work_rules(self, todo: dict) -> dict:
        """Merge project-level work rules with task-level overrides."""
        coord = self._coord
        project = await coord.db.fetchrow(
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

    async def enter_testing_or_review(self, todo: dict, provider: AIProvider) -> None:
        """Route to testing phase if code work was done, otherwise skip to review."""
        coord = self._coord
        coder_count = await coord.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
            coord.todo_id,
        )
        if coder_count and coder_count > 0:
            logger.info(
                "[%s] Code work detected (%d coder subtasks), entering testing phase",
                coord.todo_id, coder_count,
            )
            await coord._testing.run(todo, provider)
        else:
            logger.info("[%s] No coder work detected, skipping testing → review", coord.todo_id)
            await coord._review.run(todo, provider)

    async def ensure_code_push(self, workspace_path: str | None) -> bool:
        """Create a pr_creator subtask if coder work completed without a PR.

        Returns True if a pr_creator subtask was created.
        """
        coord = self._coord
        if not workspace_path:
            return False

        # Skip if a PR deliverable already exists
        existing_pr = await coord.db.fetchrow(
            "SELECT id FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
            coord.todo_id,
        )
        if existing_pr:
            return False

        # Skip if a pr_creator subtask already exists
        existing_pr_task = await coord.db.fetchrow(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'pr_creator' "
            "AND status IN ('pending', 'assigned', 'running')",
            coord.todo_id,
        )
        if existing_pr_task:
            return False

        # Check for completed coder subtasks
        coder_subtasks = await coord.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
            "AND status = 'completed' ORDER BY created_at DESC",
            coord.todo_id,
        )
        if not coder_subtasks:
            return False

        all_coder_ids = [str(st["id"]) for st in coder_subtasks]
        latest_coder = coder_subtasks[0]
        target_repo_json = latest_coder.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        max_order = await coord.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            coord.todo_id,
        )

        await coord._post_system_message(
            "**Code push:** Creating PR sub-task for completed code changes..."
        )

        pr_row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6)
            RETURNING id
            """,
            coord.todo_id,
            "Create Pull Request",
            "Commit all workspace changes, push to a feature branch, and create a pull request.",
            max_order + 1,
            all_coder_ids,
            target_repo_json,
        )

        # Propagate: tasks depending on any coder should also wait for PR creation
        pr_id = str(pr_row["id"])
        for coder_id in all_coder_ids:
            await coord._lifecycle._propagate_dependencies(coder_id, [pr_id])

        return True
