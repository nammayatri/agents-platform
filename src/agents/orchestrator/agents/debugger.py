"""Debugger agent — investigates bugs and produces findings."""
from __future__ import annotations
from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import LLMAgent


class DebuggerAgent(LLMAgent):
    role = "debugger"

    async def build_prompt(self, job, workspace, ctx, todo, *, iteration=0, iteration_log=None, work_rules=None, agent_config=None, cached_repo_map=None):
        from agents.orchestrator.context_helpers import (
            get_role_system_prompt, get_workspace_context,
            get_todo_summary, get_iteration_context,
        )

        system_parts = [await get_role_system_prompt("debugger", ctx.db, todo, agent_config)]

        if workspace:
            ws_ctx = await get_workspace_context(workspace, cached_repo_map=cached_repo_map)
            system_parts.append(ws_ctx["file_tree"])

        # Include debug context if available
        project = await ctx.load_project(str(todo["project_id"]))
        if project:
            import json
            settings = project.get("settings_json") or {}
            if isinstance(settings, str):
                settings = json.loads(settings)
            debug_ctx = settings.get("debug_context")
            if debug_ctx:
                system_parts.append(f"## Debug Context\n{debug_ctx}")

        user_parts = [get_todo_summary(todo)]
        user_parts.append(f"## Task\n{job['description'] or job['title']}")

        if iteration > 0 and iteration_log:
            user_parts.append(get_iteration_context(iteration_log, iteration))

        return {
            "system": "\n\n".join(system_parts),
            "user": "\n\n".join(user_parts),
        }

    def decide_spawn(self, job, output):
        """Debugger doesn't spawn follow-up jobs."""
        return []
