"""Subtask lifecycle — thin facade delegating to handlers/.

All logic lives in the handler modules. This file preserves the public
API so callers in execution.py, testing.py, and review.py don't change.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agents.orchestrator.handlers import COMPLETION_HANDLERS, EXECUTION_HANDLERS
from agents.orchestrator.handlers._base import HandlerContext
from agents.orchestrator.handlers._shared import (
    MAX_REVIEW_ROUNDS,
    create_guardrail_subtask as _create_guardrail_subtask,
    create_pr_creator_subtask as _create_pr_creator_subtask,
    ensure_coding_guardrails as _ensure_coding_guardrails,
    finalize_subtask_workspace as _finalize_subtask_workspace,
    propagate_dependencies as _propagate_dependencies,
)

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)


class SubtaskLifecycle:
    """Thin facade — all handler logic lives in handlers/."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    def _ctx(self) -> HandlerContext:
        """Build a HandlerContext from the coordinator."""
        c = self._coord
        return HandlerContext(
            todo_id=c.todo_id,
            db=c.db,
            redis=c.redis,
            workspace_mgr=c.workspace_mgr,
            transition_subtask=c._transition_subtask,
            post_system_message=c._post_system_message,
            report_progress=c._report_progress,
            report_activity=c._report_activity,
            load_todo=c._load_todo,
            notifier=c.notifier,
        )

    # ── Entry point for the review-merge loop ─────────────────────

    async def handle_subtask_completion(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """After a sub-task completes, dispatch to the appropriate handler."""
        coord = self._coord

        # Reload sub-task to get latest status
        st = await coord.db.fetchrow(
            "SELECT * FROM sub_tasks WHERE id = $1", sub_task["id"]
        )
        if not st or st["status"] != "completed":
            return

        role = st["agent_role"]
        is_review_loop_task = st.get("review_loop")
        is_chained_reviewer = (role == "reviewer" and st.get("review_chain_id"))
        if not is_review_loop_task and not is_chained_reviewer:
            return

        chain_id = st.get("review_chain_id") or st["id"]

        # Chain cap guard
        chain_count = await coord.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks WHERE review_chain_id = $1",
            chain_id,
        )
        if chain_count >= MAX_REVIEW_ROUNDS * 2:
            logger.warning(
                "Review chain %s hit max rounds (%d sub-tasks), stopping",
                chain_id, chain_count,
            )
            ctx = self._ctx()
            await ctx.post_system_message(
                f"**Review chain capped at {MAX_REVIEW_ROUNDS} rounds.** "
                "Creating PR sub-task with current state."
            )
            await _create_pr_creator_subtask(ctx, st, chain_id)
            return

        handler = COMPLETION_HANDLERS.get(role)
        if handler:
            await handler(self._ctx(), dict(st), provider, workspace_path)

    # ── Procedural execution delegates ────────────────────────────

    async def execute_pr_creator_subtask(self, st, provider, workspace_path):
        await EXECUTION_HANDLERS["pr_creator"](self._ctx(), st, provider, workspace_path)

    async def execute_merge_subtask(self, st, provider, workspace_path):
        await EXECUTION_HANDLERS["merge_agent"](self._ctx(), st, provider, workspace_path)

    async def execute_merge_observer_subtask(self, st, provider, workspace_path):
        await EXECUTION_HANDLERS["merge_observer"](self._ctx(), st, provider, workspace_path)

    async def execute_build_watcher_subtask(self, st, provider, workspace_path):
        await EXECUTION_HANDLERS["release_build_watcher"](self._ctx(), st, provider, workspace_path)

    async def execute_release_deployer_subtask(self, st, provider, workspace_path):
        await EXECUTION_HANDLERS["release_deployer"](self._ctx(), st, provider, workspace_path)

    # ── Shared methods (called by execution.py, testing.py, review.py) ──

    async def ensure_coding_guardrails(self, workspace_path: str | None) -> bool:
        return await _ensure_coding_guardrails(self._ctx(), workspace_path)

    async def create_guardrail_subtask(self, **kw) -> str:
        return await _create_guardrail_subtask(self._ctx(), **kw)

    async def finalize_subtask_workspace(self, sub_task, workspace_path):
        return await _finalize_subtask_workspace(self._ctx(), sub_task, workspace_path)

    async def _propagate_dependencies(self, parent_id, new_ids):
        await _propagate_dependencies(self._ctx(), parent_id, new_ids)
