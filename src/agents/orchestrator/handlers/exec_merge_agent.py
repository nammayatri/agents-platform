"""Execution handler — merge agent (check CI, merge PR, run post-merge builds)."""

from __future__ import annotations

import json
import logging

from agents.orchestrator.handlers._base import HandlerContext

logger = logging.getLogger(__name__)


async def _verify_merge_authorization(ctx: HandlerContext, pr_number: int) -> None:
    """Final guard: re-verify approval from DB before irreversible merge."""
    row = await ctx.db.fetchrow(
        "SELECT t.sub_state, p.settings_json "
        "FROM todo_items t JOIN projects p ON t.project_id = p.id "
        "WHERE t.id = $1",
        ctx.todo_id,
    )
    from agents.utils.settings_helpers import parse_settings, read_setting
    proj_settings = parse_settings(row["settings_json"])

    require = read_setting(proj_settings, "git.require_merge_approval", "require_merge_approval", False)
    sub_state = row["sub_state"]

    if require and sub_state not in ("merge_approved", "merging"):
        logger.error(
            "MERGE BLOCKED: todo=%s pr=#%d require_merge_approval=True sub_state=%s",
            ctx.todo_id[:8], pr_number, sub_state,
        )
        raise RuntimeError(
            f"Merge not authorized: approval required but sub_state is '{sub_state}'"
        )

    logger.info(
        "Merge authorization passed: todo=%s pr=#%d require=%s sub_state=%s",
        ctx.todo_id[:8], pr_number, require, sub_state,
    )


async def execute_merge_agent(
    ctx: HandlerContext, sub_task: dict, provider, workspace_path: str | None,
) -> None:
    """Procedural: check CI, merge PR, run post-merge builds."""
    from agents.orchestrator.handlers._shared import (
        create_release_subtasks,
        resolve_git_for_subtask,
        run_post_merge_builds,
    )

    st_id = str(sub_task["id"])
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = CASE "
        "WHEN sub_state = 'merge_approved' THEN 'merge_approved' "
        "ELSE 'merging' END, updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
    )

    try:
        todo = await ctx.load_todo()
        project = await ctx.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", todo["project_id"]
        )
        if not project or not project.get("repo_url"):
            raise ValueError("No repo configured for merge")

        # Resolve target_repo to filter the correct PR deliverable
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo)
        target_repo_name = target_repo.get("name") if target_repo else None

        if target_repo_name:
            pr_deliv = await ctx.db.fetchrow(
                "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "AND pr_number IS NOT NULL AND target_repo_name = $2 "
                "ORDER BY created_at DESC LIMIT 1",
                ctx.todo_id, target_repo_name,
            )
        else:
            pr_deliv = await ctx.db.fetchrow(
                "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "AND pr_number IS NOT NULL AND target_repo_name IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                ctx.todo_id,
            )
        if not pr_deliv:
            await ctx.post_system_message("**Merge agent:** No PR found to merge. Skipping.")
            await ctx.transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message="No PR to merge",
            )
            return

        # Resolve git provider from subtask's target_repo (dep) or project (main)
        git, owner, repo = await resolve_git_for_subtask(ctx, sub_task, project)
        pr_number = pr_deliv["pr_number"]

        await ctx.report_progress(st_id, 20, "Checking PR status")

        # 1. Get PR status
        pr_data = await git.get_pull_request(owner, repo, pr_number)
        if pr_data["state"] != "open":
            await ctx.post_system_message(
                f"**Merge agent:** PR #{pr_number} is {pr_data['state']}, not open. Skipping merge."
            )
            await ctx.transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"PR already {pr_data['state']}",
            )
            return

        # 2. Check CI status
        await ctx.report_progress(st_id, 40, "Checking CI status")
        ci_data = await git.get_check_runs(owner, repo, pr_data["head_sha"])

        if ci_data["state"] == "pending":
            await ctx.post_system_message(
                f"**Merge agent:** CI still running for PR #{pr_number}. Will retry on next cycle."
            )
            await ctx.transition_subtask(
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
            await ctx.post_system_message(
                f"**Merge agent:** CI failed for PR #{pr_number}. {msg}"
            )
            await ctx.transition_subtask(
                st_id, "failed",
                error_message=msg,
            )
            return

        # 3. Check for unmerged dependency PRs
        await ctx.report_progress(st_id, 50, "Checking dependency PRs")
        dep_subtasks = await ctx.db.fetch(
            "SELECT d.pr_state, d.target_repo_name, d.pr_number "
            "FROM sub_tasks st JOIN deliverables d ON d.sub_task_id = st.id "
            "WHERE st.todo_id = $1 AND st.target_repo IS NOT NULL "
            "AND d.type = 'pull_request' AND d.pr_state != 'merged'",
            ctx.todo_id,
        )
        if dep_subtasks:
            dep_names = [d.get("target_repo_name") or f"PR #{d['pr_number']}" for d in dep_subtasks]
            await ctx.post_system_message(
                f"**Merge agent:** Waiting for dependency PRs: {', '.join(dep_names)}"
            )
            await ctx.transition_subtask(
                st_id, "pending",
                progress_message="Waiting for dependency PRs",
            )
            return

        # 4. Check if human approval is required
        from agents.utils.settings_helpers import get_build_command_strings, parse_settings, read_setting
        project_settings = parse_settings(project.get("settings_json"))

        require_approval = read_setting(project_settings, "git.require_merge_approval", "require_merge_approval", False)
        already_approved = todo.get("sub_state") == "merge_approved"

        logger.info(
            "Merge approval check: todo=%s pr=#%d require=%s approved=%s sub_state=%s",
            ctx.todo_id[:8], pr_number, require_approval, already_approved,
            todo.get("sub_state"),
        )

        if require_approval and not already_approved:
            await ctx.db.execute(
                "UPDATE todo_items SET sub_state = 'awaiting_merge_approval', updated_at = NOW() WHERE id = $1",
                ctx.todo_id,
            )
            await ctx.post_system_message(
                f"**PR #{pr_number} is ready to merge.** CI passed. Awaiting your approval to merge."
            )
            await ctx.transition_subtask(
                st_id, "pending",
                progress_message="Awaiting merge approval",
            )
            await ctx.redis.publish(
                f"task:{ctx.todo_id}:events",
                json.dumps({
                    "type": "state_change",
                    "state": "in_progress",
                    "sub_state": "awaiting_merge_approval",
                }),
            )
            return

        if already_approved:
            await ctx.db.execute(
                "UPDATE todo_items SET sub_state = 'merging', updated_at = NOW() WHERE id = $1",
                ctx.todo_id,
            )

        # 5. Merge the PR
        await ctx.report_progress(st_id, 70, f"Merging PR #{pr_number}")
        merge_method = read_setting(project_settings, "git.merge_method", "merge_method", "squash")

        await _verify_merge_authorization(ctx, pr_number)

        merge_result = await git.merge_pull_request(
            owner, repo, pr_number, method=merge_method,
        )

        if not merge_result.get("merged"):
            await ctx.post_system_message(
                f"**Merge agent:** Failed to merge PR #{pr_number}: {merge_result.get('message', 'unknown error')}"
            )
            await ctx.transition_subtask(
                st_id, "failed",
                error_message=merge_result.get("message", "Merge failed"),
            )
            return

        # 6. Update deliverable
        await ctx.db.execute(
            "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
            "merge_method = $2, status = 'approved' WHERE id = $1",
            pr_deliv["id"],
            merge_method,
        )

        await ctx.post_system_message(
            f"**PR #{pr_number} merged** via {merge_method}. SHA: {merge_result.get('sha', 'N/A')}"
        )

        # 7. Post-merge build commands
        build_commands = get_build_command_strings(project_settings)
        if build_commands and workspace_path:
            await ctx.report_progress(st_id, 85, "Running post-merge builds")
            await run_post_merge_builds(ctx, todo, build_commands, workspace_path)

        await ctx.report_progress(st_id, 100, "Merge complete")
        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100, progress_message="Merged",
        )

        # Trigger release pipeline if enabled
        if read_setting(project_settings, "release.enabled", "release_pipeline_enabled", False):
            try:
                await create_release_subtasks(ctx, sub_task, project_settings)
            except Exception as e:
                logger.error("Failed to create release subtasks: %s", e, exc_info=True)
                await ctx.post_system_message(
                    f"**Release pipeline:** Failed to create release sub-tasks: {str(e)[:300]}"
                )

    except Exception as e:
        logger.error("Merge agent failed: %s", e, exc_info=True)
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=str(e)[:500],
        )
