"""Planner agent — wraps the existing PlanningPhase logic.

The planning phase is ~1000 lines of complex exploration + plan generation
logic that is tightly coupled to the coordinator. Rather than rewriting it,
this agent creates a lightweight adapter so the scheduler can invoke it
through the standard BaseAgent interface.

The planning logic will be gradually inlined into this agent as the
coordinator is phased out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agents.orchestrator.agent_result import AgentResult
from agents.orchestrator.agents._base import BaseAgent

if TYPE_CHECKING:
    from agents.orchestrator.run_context import RunContext

logger = logging.getLogger(__name__)


class _CoordinatorAdapter:
    """Minimal adapter making RunContext look like AgentCoordinator.

    Only implements the subset of coordinator methods that PlanningPhase
    actually calls. This lets us reuse the 1000+ line planning logic
    unchanged while the scheduler uses RunContext everywhere else.
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self.todo_id = ctx.todo_id
        self.db = ctx.db
        self.redis = ctx.redis
        self.workspace_mgr = ctx.workspace_mgr
        self.mcp_executor = ctx.mcp_executor
        self.tools_registry = ctx.tools_registry
        self.provider_registry = ctx.provider_registry
        self.notifier = ctx.notifier
        self._chat_session_id = ctx.chat_session_id

        # These are used by PlanningPhase for event streaming
        self._dispatcher = None

    async def _transition_todo(self, state: str, **kwargs):
        return await self._ctx.transition_todo(state, **kwargs)

    async def _post_system_message(self, content: str, metadata=None):
        await self._ctx.post_system_message(content, metadata=metadata)

    async def _track_tokens(self, response):
        await self._ctx.track_tokens(response)

    async def _is_cancelled(self):
        return await self._ctx.is_cancelled()

    async def _load_todo(self):
        return await self._ctx.load_todo()

    def _build_token_streamer(self, subtask_id=None):
        return self._ctx.build_token_streamer(subtask_id)

    async def _report_planning_activity(self, activity: str):
        await self._ctx.report_planning_activity(activity)

    async def _build_context(self, todo: dict) -> dict:
        """Build project context for the planner."""
        from agents.orchestrator.context_builder import ContextBuilder
        builder = ContextBuilder(self.db)
        return await builder.build_context(todo)

    def _get_builtin_tools(self, workspace_path: str, role: str):
        from agents.agents.registry import get_builtin_tool_schemas
        return get_builtin_tool_schemas(workspace_path, role)

    def _get_dep_index_dirs(self, workspace_path: str) -> dict[str, str]:
        import os
        deps_root = os.path.join(workspace_path, ".agent_index_deps")
        if not os.path.isdir(deps_root):
            return {}
        result = {}
        try:
            for entry in os.listdir(deps_root):
                idx = os.path.join(deps_root, entry)
                if os.path.isdir(idx):
                    result[entry] = idx
        except OSError:
            pass
        return result


class PlannerAgent(BaseAgent):
    """Explore codebase and decompose task into sub-tasks.

    Delegates to the existing PlanningPhase logic via _CoordinatorAdapter.
    """

    role = "planner"

    async def run(self, job: dict, workspace: str | None, ctx: RunContext) -> AgentResult:
        """Run the planning phase.

        The `job` parameter here is the todo_items row, not a sub_tasks row.
        The workspace parameter is not used (planner sets up its own workspace).
        """
        from agents.orchestrator.phases.planning import PlanningPhase

        adapter = _CoordinatorAdapter(ctx)
        planning = PlanningPhase(adapter)

        todo = await ctx.load_todo()
        provider = await ctx.provider_registry.resolve_for_todo(ctx.todo_id)

        await planning.run(todo, provider)

        # The planning phase handles everything internally:
        # - Creates sub-tasks in DB
        # - Transitions todo to plan_ready or in_progress
        # - May start execution immediately (auto-approve)
        # Return empty result since planning is self-contained
        return AgentResult(output={"phase": "planning", "completed": True})
