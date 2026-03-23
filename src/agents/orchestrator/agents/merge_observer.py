"""Merge observer — polls for external PR merge."""

from __future__ import annotations

import logging

from agents.orchestrator.agent_result import AgentResult
from agents.orchestrator.agents._base import BaseAgent

logger = logging.getLogger(__name__)


class MergeObserverAgent(BaseAgent):
    """Procedural agent: poll for external PR merge until merged or closed.

    Delegates to the existing exec_merge_observer handler to preserve exact
    behavior.  The handler code can be inlined into this class later.
    """

    role = "merge_observer"

    async def run(self, job, workspace, ctx) -> AgentResult:
        """Poll PR status until it is merged or closed externally."""
        from agents.orchestrator.handlers._base import HandlerContext
        from agents.orchestrator.handlers.exec_merge_observer import execute_merge_observer

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
        await execute_merge_observer(handler_ctx, job, provider, workspace)

        # Reload job to get output_result set by handler
        updated = await ctx.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE id = $1", job["id"],
        )
        output = (updated.get("output_result") or {}) if updated else {}

        return AgentResult(output=output)
