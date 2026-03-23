"""PR Creator agent — commits, pushes, creates pull request."""

from __future__ import annotations

import json
import logging

from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import BaseAgent

logger = logging.getLogger(__name__)


class PrCreatorAgent(BaseAgent):
    """Procedural agent: commit workspace changes, push branch, create PR.

    Delegates to the existing exec_pr_creator handler to preserve exact
    behavior.  The handler code can be inlined into this class later.
    """

    role = "pr_creator"

    async def run(self, job, workspace, ctx) -> AgentResult:
        """Commit workspace changes, push branch, create PR."""
        from agents.orchestrator.handlers._base import HandlerContext
        from agents.orchestrator.handlers.exec_pr_creator import execute_pr_creator

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
        await execute_pr_creator(handler_ctx, job, provider, workspace)

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
        """After PR creation, spawn merge agent or observer."""
        pr_url = output.get("pr_url")
        if not pr_url:
            return []

        merge_strategy = output.get("merge_strategy", "auto")
        chain_id = job.get("review_chain_id") or str(job["id"])
        target_repo = job.get("target_repo")

        if merge_strategy == "external":
            return [
                JobSpec(
                    title="Monitor PR merge",
                    description=f"Poll PR {pr_url} until it is merged or closed externally.",
                    role="merge_observer",
                    chain_id=chain_id,
                    target_repo=target_repo,
                ),
            ]
        elif merge_strategy == "auto":
            return [
                JobSpec(
                    title="Merge PR",
                    description=f"Wait for CI checks on {pr_url}, then merge.",
                    role="merge_agent",
                    chain_id=chain_id,
                    target_repo=target_repo,
                ),
            ]

        # merge_strategy == "manual" or unknown — no auto-merge
        return []
