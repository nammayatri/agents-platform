"""Build watcher agent — monitors CI builds after merge."""

from __future__ import annotations

import logging

from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import BaseAgent

logger = logging.getLogger(__name__)


class BuildWatcherAgent(BaseAgent):
    """Procedural agent: monitor CI builds post-merge (GitHub Actions, Jenkins).

    Delegates to the existing exec_build_watcher handler to preserve exact
    behavior.  The handler code can be inlined into this class later.
    """

    role = "release_build_watcher"

    async def run(self, job, workspace, ctx) -> AgentResult:
        """Monitor CI build pipeline and report pass/fail."""
        from agents.orchestrator.handlers._base import HandlerContext
        from agents.orchestrator.handlers.exec_build_watcher import execute_build_watcher

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
        await execute_build_watcher(handler_ctx, job, provider, workspace)

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
        """After successful build, spawn deployer if deploy config exists."""
        spawn: list[JobSpec] = []

        if output.get("build_passed") and output.get("deploy_config"):
            spawn.append(
                JobSpec(
                    title="Deploy release",
                    description="Trigger deployment for the successful build.",
                    role="release_deployer",
                    target_repo=job.get("target_repo"),
                ),
            )

        return spawn
