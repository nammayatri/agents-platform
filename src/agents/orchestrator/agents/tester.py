"""Tester agent — runs tests and spawns fix coders on failure."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agents.orchestrator.agent_result import JobSpec
from agents.orchestrator.agents._base import LLMAgent

if TYPE_CHECKING:
    from agents.orchestrator.run_context import RunContext

logger = logging.getLogger(__name__)

_MAX_TEST_FIX_ROUNDS = 3  # Max tester -> fix -> retest cycles per chain


class TesterAgent(LLMAgent):
    role = "tester"

    async def build_prompt(
        self,
        job,
        workspace,
        ctx,
        todo,
        *,
        iteration=0,
        iteration_log=None,
        work_rules=None,
        agent_config=None,
        cached_repo_map=None,
    ):
        """Build testing prompt with workspace context and test commands."""
        from agents.orchestrator.context_helpers import (
            get_role_system_prompt,
            get_workspace_context,
            get_todo_summary,
            get_iteration_context,
            get_work_rules_prompt,
        )

        system_parts = [await get_role_system_prompt("tester", ctx.db, todo, agent_config)]

        if workspace:
            ws_ctx = await get_workspace_context(workspace, cached_repo_map=cached_repo_map)
            system_parts.append(ws_ctx["file_tree"])

        if work_rules:
            system_parts.append(get_work_rules_prompt(work_rules))

        user_parts = [get_todo_summary(todo)]
        user_parts.append(f"## Task\n{job['description'] or job['title']}")

        if iteration > 0 and iteration_log:
            user_parts.append(get_iteration_context(iteration_log, iteration))

        return {
            "system": "\n\n".join(system_parts),
            "user": "\n\n".join(user_parts),
        }

    def decide_spawn(self, job, output):
        """Determine follow-up jobs after testing.

        - All tests pass: return [] (no follow-up needed).
        - Failures found: spawn fix coders grouped by failure type + a
          re-tester that depends on all fix coders.
        - Round cap: max 3 tester -> fix -> retest cycles per chain.
        """
        passed = output.get("passed", True)
        failures = output.get("failures", [])

        if passed or not failures:
            return []

        chain_id = job.get("review_chain_id") or str(job["id"])
        target_repo = job.get("target_repo")
        summary = output.get("summary", "")

        # ── Group failures by type ──
        by_type: dict[str, list] = {}
        for f in failures:
            ftype = f.get("type", "test") if isinstance(f, dict) else "test"
            by_type.setdefault(ftype, []).append(f)

        type_labels = {
            "build": "Build errors",
            "type_error": "Type errors",
            "test": "Test failures",
            "lint": "Lint violations",
            "runtime": "Runtime errors",
        }

        # ── One fix-coder per failure type ──
        spawns: list[JobSpec] = []
        for ftype, type_failures in by_type.items():
            label = type_labels.get(ftype, ftype)

            error_lines: list[str] = []
            for f in type_failures:
                if not isinstance(f, dict):
                    error_lines.append(f"- {f}")
                    continue
                file_path = f.get("file", "")
                error_msg = f.get("error", str(f))
                if file_path:
                    error_lines.append(f"**{file_path}**:\n```\n{error_msg[:1000]}\n```")
                else:
                    error_lines.append(f"```\n{error_msg[:1000]}\n```")

            fix_desc = (
                f"The tester found {len(type_failures)} {label.lower()} that need to be fixed:\n\n"
                + "\n\n".join(error_lines)
                + "\n\nFix the actual problems in the source code. "
                "Do NOT disable tests, skip checks, or suppress errors."
            )

            spawns.append(JobSpec(
                title=f"Fix {label.lower()}",
                description=fix_desc,
                role="coder",
                chain_id=chain_id,
                target_repo=target_repo,
            ))

        # ── Re-tester that depends on all fix coders ──
        retest_desc = (
            "Re-run all build, typecheck, lint, and test commands to verify "
            "the fixes resolved the previously reported failures.\n\n"
            f"Previous failures summary: {summary[:500]}\n\n"
            "Run ALL checks and report structured results."
        )
        spawns.append(JobSpec(
            title="Re-test after fixes",
            description=retest_desc,
            role="tester",
            depends_on_parent=False,
            depends_on_siblings=True,
            review_loop=True,
            chain_id=chain_id,
            target_repo=target_repo,
        ))

        return spawns
