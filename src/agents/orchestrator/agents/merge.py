"""Merge agent — waits for CI, merges PR."""

from __future__ import annotations

import logging

from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import BaseAgent

logger = logging.getLogger(__name__)


class MergeAgent(BaseAgent):
    """Procedural agent: check CI status, merge PR, run post-merge builds.

    Delegates to the existing exec_merge_agent handler to preserve exact
    behavior.  The handler code can be inlined into this class later.
    """

    role = "merge_agent"

    async def run(self, job, workspace, ctx) -> AgentResult:
        """Wait for CI checks then merge the PR."""
        from agents.orchestrator.handlers._base import HandlerContext
        from agents.orchestrator.handlers.exec_merge_agent import execute_merge_agent

        handler_ctx = HandlerContext(
            todo_id=ctx.todo_id,
            db=ctx.db,
            redis=ctx.redis,
            workspace_mgr=ctx.workspace_mgr,
            transition_subtask=ctx.transition_subtask,
            post_system_message=ctx.post_system_message,
            report_progress=ctx.report_progress,
            report_activity=ctx.report_activity,
            load_todo=ctx.load_todo,
            notifier=ctx.notifier,
        )

        provider = await ctx.provider_registry.resolve_for_todo(ctx.todo_id)
        await execute_merge_agent(handler_ctx, job, provider, workspace)

        # Reload job to get output_result set by handler
        updated = await ctx.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE id = $1", job["id"],
        )
        output = (updated.get("output_result") or {}) if updated else {}

        return AgentResult(output=output, spawn=self._decide_spawn(job, output))

    # ------------------------------------------------------------------
    # Spawn logic
    # ------------------------------------------------------------------

    def _decide_spawn(self, job: dict, output: dict) -> list[JobSpec]:
        """After merge, spawn release pipeline if configured."""
        if not output.get("merged"):
            return []

        release_config = output.get("release_config")
        if not release_config:
            return []

        return [
            JobSpec(
                title="Monitor release build",
                description="Watch CI build triggered by merge and report status.",
                role="release_build_watcher",
                target_repo=job.get("target_repo"),
            ),
        ]
