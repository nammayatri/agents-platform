"""Coder agent — writes/modifies code."""
from __future__ import annotations
from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import LLMAgent


class CoderAgent(LLMAgent):
    role = "coder"

    async def build_prompt(self, job, workspace, ctx, todo, *, iteration=0, iteration_log=None, work_rules=None, agent_config=None, cached_repo_map=None):
        """Build coder prompt using context helpers."""
        from agents.orchestrator.context_helpers import (
            get_role_system_prompt, get_workspace_context,
            get_previous_results, get_todo_summary,
            get_iteration_context, get_work_rules_prompt,
        )

        system_parts = [await get_role_system_prompt("coder", ctx.db, todo, agent_config)]

        if workspace:
            ws_ctx = await get_workspace_context(workspace, cached_repo_map=cached_repo_map)
            system_parts.append(ws_ctx["file_tree"])
            if ws_ctx.get("repo_map"):
                system_parts.append(ws_ctx["repo_map"])

        if work_rules:
            system_parts.append(get_work_rules_prompt(work_rules))

        user_parts = [get_todo_summary(todo)]
        user_parts.append(f"## Task\n{job['description'] or job['title']}")

        if iteration > 0 and iteration_log:
            user_parts.append(get_iteration_context(iteration_log, iteration))
        else:
            prev = await get_previous_results(ctx.db, ctx.todo_id)
            if prev:
                user_parts.append(prev)

        return {
            "system": "\n\n".join(system_parts),
            "user": "\n\n".join(user_parts),
        }

    def decide_spawn(self, job, output):
        """After coder completes, create reviewer if review_loop is enabled."""
        if not job.get("review_loop"):
            return []

        chain_id = job.get("review_chain_id") or str(job["id"])
        target_repo = job.get("target_repo")

        return [JobSpec(
            title=f"Review: {job['title']}",
            description="Review the code changes and verify correctness, style, and completeness.",
            role="reviewer",
            depends_on_parent=True,
            chain_id=chain_id,
            target_repo=target_repo,
        )]
