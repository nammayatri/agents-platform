"""Execution handler — PR creator (commit, push, create PR, optionally spawn merge_agent)."""

from __future__ import annotations

import json
import logging

from agents.orchestrator.handlers._base import HandlerContext

logger = logging.getLogger(__name__)


async def execute_pr_creator(
    ctx: HandlerContext, sub_task: dict, provider, workspace_path: str | None,
) -> None:
    """Procedural: commit, push, create PR, optionally create merge_agent."""
    from agents.orchestrator.handlers._shared import (
        build_pre_commit_fix_description,
        create_guardrail_subtask,
        finalize_subtask_workspace,
        propagate_dependencies,
        requires_merge_approval,
        resolve_git_for_subtask,
    )

    st_id = str(sub_task["id"])
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = 'creating_pr', updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
    )

    try:
        if not workspace_path:
            raise ValueError("No workspace path available for PR creation")

        await ctx.report_progress(st_id, 10, "Preparing to commit and push")

        coder_st = await ctx.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
            "AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            ctx.todo_id,
        )
        commit_st = coder_st or sub_task

        await ctx.report_progress(st_id, 30, "Committing and pushing changes")
        pr_info = await finalize_subtask_workspace(ctx, commit_st, workspace_path)

        if not pr_info:
            raise ValueError("PR creation failed — no PR data returned")

        # Handle pre-commit hook failure — create a fix subtask and retry
        if pr_info.get("_pre_commit_failed"):
            pre_commit_output = pr_info.get("pre_commit_output", "")
            logger.info("[%s] Pre-commit hooks failed, creating fix subtask", ctx.todo_id)

            fix_description = build_pre_commit_fix_description(pre_commit_output)
            fix_id = await create_guardrail_subtask(
                ctx,
                title="Fix: pre-commit hook errors",
                description=fix_description,
                role="coder",
                depends_on=[],
            )

            await ctx.post_system_message(
                f"**Pre-commit hooks failed.** Created fix subtask to resolve the errors.\n\n"
                f"```\n{pre_commit_output[:2000]}\n```"
            )

            # Reset PR creator to pending, blocked on the fix subtask
            await ctx.db.execute(
                "UPDATE sub_tasks SET status = 'pending', depends_on = $2, updated_at = NOW() WHERE id = $1",
                sub_task["id"],
                [fix_id],
            )
            logger.info(
                "[%s] PR creator %s reset to pending, blocked on fix subtask %s",
                ctx.todo_id, st_id, fix_id,
            )
            return

        pr_url = pr_info.get("url", "N/A")
        await ctx.report_progress(st_id, 80, f"PR created: {pr_url}")

        # Store head_sha for release pipeline
        pr_number = pr_info.get("number")
        if pr_number:
            try:
                todo_pr = await ctx.load_todo()
                project_pr = await ctx.db.fetchrow(
                    "SELECT * FROM projects WHERE id = $1", todo_pr["project_id"]
                )
                # Use subtask's target_repo to resolve the correct git provider
                pr_git, pr_owner, pr_repo = await resolve_git_for_subtask(
                    ctx, sub_task, project_pr,
                )
                pr_data = await pr_git.get_pull_request(pr_owner, pr_repo, pr_number)
                if pr_data and pr_data.get("head_sha"):
                    await ctx.db.execute(
                        "UPDATE deliverables SET head_sha = $2 "
                        "WHERE todo_id = $1 AND type = 'pull_request' AND pr_number = $3",
                        ctx.todo_id, pr_data["head_sha"], pr_number,
                    )
                    logger.info("[%s] Stored head_sha %s for PR #%s", ctx.todo_id, pr_data["head_sha"][:12], pr_number)
            except Exception as e:
                logger.warning("[%s] Failed to store head_sha for PR: %s", ctx.todo_id, e)

        if await requires_merge_approval(ctx):
            # No merge subtask — user merges the PR manually
            await ctx.post_system_message(
                f"**PR created:** {pr_url}\n\n"
                "Auto-merge is disabled. Review and merge the PR manually when ready."
            )
            await ctx.transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"PR: {pr_url}",
            )
            return

        # Auto-merge: create merge_agent subtask
        all_coder_ids = [str(sub_task["id"])]
        if coder_st:
            all_coder_ids.append(str(coder_st["id"]))

        target_repo_json = sub_task.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        max_order = await ctx.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            ctx.todo_id,
        )

        await ctx.post_system_message(
            f"**PR created:** {pr_url}. Creating merge sub-task."
        )

        merge_row = await ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'merge_agent', $4, $5, $6)
            RETURNING id
            """,
            ctx.todo_id,
            "Merge PR",
            "Merge the PR. Check CI status, merge, and run post-merge builds if configured.",
            max_order + 1,
            all_coder_ids,
            target_repo_json,
        )

        # Propagate: tasks depending on pr_creator should also wait for merge
        await propagate_dependencies(ctx, st_id, [str(merge_row["id"])])

        await ctx.report_progress(st_id, 100, "PR created successfully")
        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100, progress_message=f"PR: {pr_url}",
        )

    except Exception as e:
        logger.error("[%s] PR creator failed: %s", ctx.todo_id, e, exc_info=True)
        await ctx.post_system_message(
            f"**PR creation failed:** {str(e)[:500]}. You can retry this sub-task."
        )
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=str(e)[:500],
        )
