"""Execution handler — merge observer (poll for external PR merge)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from agents.orchestrator.handlers._base import HandlerContext

logger = logging.getLogger(__name__)


async def _check_pr_merged(git, owner: str, repo: str, pr_number: int, pr_data: dict) -> bool:
    """Check if a closed PR was actually merged (not just closed)."""
    # GitLab: state == "merged" directly
    if pr_data.get("state") == "merged":
        return True
    # GitHub: check merge endpoint (204 = merged, 404 = not merged)
    if hasattr(git, "http"):
        try:
            resp = await git.http.get(
                f"{git.api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            )
            return resp.status_code == 204
        except Exception:
            pass
    # Fallback: check merged flag
    return bool(pr_data.get("merged"))


async def execute_merge_observer(
    ctx: HandlerContext, sub_task: dict, provider, workspace_path: str | None,
) -> None:
    """Procedural: poll for external PR merge."""
    from agents.orchestrator.handlers._shared import (
        create_release_subtasks,
        resolve_git_for_subtask,
    )

    st_id = str(sub_task["id"])
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = 'awaiting_external_merge', "
        "updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
    )

    try:
        todo = await ctx.load_todo()
        project = await ctx.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", todo["project_id"]
        )
        if not project or not project.get("repo_url"):
            raise ValueError("No repo configured for merge observation")

        # Resolve target_repo for correct PR lookup
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
            raise ValueError("No PR deliverable found to observe")

        git, owner, repo = await resolve_git_for_subtask(ctx, sub_task, project)
        pr_number = pr_deliv["pr_number"]

        await ctx.post_system_message(
            f"**Merge observer:** Watching PR #{pr_number} for external merge. "
            "Auto-merge is disabled — a human must merge this PR."
        )

        await ctx.redis.publish(
            f"task:{ctx.todo_id}:events",
            json.dumps({
                "type": "state_change",
                "state": "in_progress",
                "sub_state": "awaiting_external_merge",
            }),
        )

        poll_interval = 3600   # 1 hour
        timeout_hours = 72     # 3 days
        deadline = time.monotonic() + timeout_hours * 3600
        wake_key = f"merge_observer:{ctx.todo_id}:wake"

        while time.monotonic() < deadline:
            # Clear any pending wake signal
            await ctx.redis.delete(wake_key)

            # Poll PR status
            pr_data = await git.get_pull_request(owner, repo, pr_number)

            if pr_data.get("state") in ("closed", "merged"):
                merged = await _check_pr_merged(git, owner, repo, pr_number, pr_data)
                if merged:
                    await ctx.db.execute(
                        "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
                        "status = 'approved' WHERE id = $1",
                        pr_deliv["id"],
                    )
                    merge_sha = pr_data.get("merge_commit_sha", pr_data.get("head_sha", ""))
                    await ctx.post_system_message(
                        f"**PR #{pr_number} merged externally.** SHA: {merge_sha[:12]}"
                    )
                    await ctx.transition_subtask(
                        st_id, "completed",
                        progress_pct=100, progress_message="Merged externally",
                    )
                    await ctx.db.execute(
                        "UPDATE todo_items SET sub_state = NULL, updated_at = NOW() WHERE id = $1",
                        ctx.todo_id,
                    )

                    # Trigger release pipeline if enabled
                    project_settings = project.get("settings_json") or {}
                    if isinstance(project_settings, str):
                        project_settings = json.loads(project_settings)
                    if project_settings.get("release_pipeline_enabled"):
                        try:
                            await create_release_subtasks(ctx, sub_task, project_settings)
                        except Exception as rel_e:
                            logger.error("[%s] Failed to create release subtasks: %s", ctx.todo_id, rel_e)
                    return
                else:
                    # PR closed without merge
                    await ctx.post_system_message(
                        f"**PR #{pr_number} was closed without merging.**"
                    )
                    await ctx.transition_subtask(
                        st_id, "failed",
                        error_message="PR closed without merge",
                    )
                    return

            # Still open — update progress and keep alive
            await ctx.report_progress(
                st_id, 20,
                f"PR #{pr_number} still open. Next check in {poll_interval // 60}m.",
            )
            await ctx.db.execute(
                "UPDATE todo_items SET updated_at = NOW() WHERE id = $1",
                ctx.todo_id,
            )

            # Sleep in short increments, checking for webhook wake signal
            sleep_end = time.monotonic() + poll_interval
            while time.monotonic() < sleep_end and time.monotonic() < deadline:
                woken = await ctx.redis.get(wake_key)
                if woken:
                    await ctx.redis.delete(wake_key)
                    logger.info("[%s] Merge observer woken by webhook", ctx.todo_id)
                    break
                await asyncio.sleep(30)

        # Timed out
        await ctx.post_system_message(
            f"**Merge observer:** Timed out after {timeout_hours}h waiting for PR #{pr_number}."
        )
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=f"Timed out after {timeout_hours}h waiting for external merge",
        )

    except Exception as e:
        logger.error("[%s] Merge observer failed: %s", ctx.todo_id, e, exc_info=True)
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=str(e)[:500],
        )
