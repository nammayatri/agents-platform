"""Reviewer agent — reviews code and decides what happens next.

The reviewer is one of the most critical agents in the orchestrator.
After reviewing workspace changes it either:
  - Approves and spawns a pr_creator job, or
  - Requests changes and spawns fix coder(s) + a re-reviewer.

The spawn logic mirrors the handler in on_reviewer_complete.py but
expressed declaratively via JobSpec so the scheduler can wire
dependencies automatically.
"""

from __future__ import annotations

import logging

from agents.orchestrator.agent_result import AgentResult, JobSpec
from agents.orchestrator.agents._base import LLMAgent

logger = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 10


class ReviewerAgent(LLMAgent):
    role = "reviewer"

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
        """Build the reviewer prompt with workspace context and review criteria."""
        from agents.orchestrator.context_helpers import (
            get_role_system_prompt,
            get_workspace_context,
            get_todo_summary,
        )

        system_parts = [
            await get_role_system_prompt("reviewer", ctx.db, todo, agent_config)
        ]

        if workspace:
            ws_ctx = await get_workspace_context(
                workspace, cached_repo_map=cached_repo_map,
            )
            system_parts.append(ws_ctx["file_tree"])
            if ws_ctx.get("repo_map"):
                system_parts.append(ws_ctx["repo_map"])

        user_parts = [get_todo_summary(todo)]
        user_parts.append(
            f"## Task to Review\n{job['description'] or job['title']}"
        )

        return {
            "system": "\n\n".join(system_parts),
            "user": "\n\n".join(user_parts),
        }

    def decide_spawn(self, job: dict, output: dict) -> list[JobSpec]:
        """Determine follow-up jobs after review completes.

        Returns:
          - If approved: a single pr_creator job.
          - If needs_changes (single file): 1 fix coder + re-reviewer.
          - If needs_changes (multi file): N parallel fix coders + re-reviewer
            that depends on all siblings.
          - If review round cap is hit: pr_creator regardless of verdict.
        """
        chain_id = job.get("review_chain_id") or str(job["id"])
        target_repo = job.get("target_repo")
        verdict = output.get("verdict", "approved")

        # ── Round cap: force PR creation if we've exhausted review rounds ──
        review_round = job.get("_review_round", 0)
        if review_round >= MAX_REVIEW_ROUNDS:
            logger.warning(
                "Review chain %s hit max rounds (%d), forcing PR creation",
                chain_id,
                review_round,
            )
            return [
                JobSpec(
                    title="Create Pull Request",
                    description=(
                        "Review round cap reached. "
                        "Commit, push, and create PR with current state."
                    ),
                    role="pr_creator",
                    chain_id=chain_id,
                    target_repo=target_repo,
                )
            ]

        # ── Approved: spawn PR creator ──
        if verdict == "approved":
            return [
                JobSpec(
                    title="Create Pull Request",
                    description=(
                        "Commit, push, create PR for the reviewed changes."
                    ),
                    role="pr_creator",
                    chain_id=chain_id,
                    target_repo=target_repo,
                )
            ]

        # ── Needs changes: spawn fix coder(s) + re-reviewer ──
        issues = output.get("issues", [])
        feedback = output.get("content", "")

        # Group issues by file
        file_groups: dict[str, list[dict]] = {}
        for issue in issues:
            if isinstance(issue, dict):
                key = issue.get("file") or "_general"
                file_groups.setdefault(key, []).append(issue)

        spawns: list[JobSpec] = []
        base_title = job["title"].removeprefix("Review: ")

        if len(file_groups) <= 1:
            # Single fix coder
            desc = _build_fix_description(feedback, issues, output)
            spawns.append(
                JobSpec(
                    title=f"Fix: {base_title}",
                    description=desc,
                    role="coder",
                    chain_id=chain_id,
                    target_repo=target_repo,
                    review_loop=True,
                )
            )
        else:
            # Parallel fix coders — one per file group
            for file_key, file_issues in file_groups.items():
                display_file = file_key if file_key != "_general" else "general"
                desc = _build_fix_description_for_file(
                    file_key, file_issues, job,
                )
                spawns.append(
                    JobSpec(
                        title=f"Fix ({display_file}): {base_title}",
                        description=desc,
                        role="coder",
                        chain_id=chain_id,
                        target_repo=target_repo,
                    )
                )

        # Re-reviewer that waits for all fix coders to complete
        spawns.append(
            JobSpec(
                title=f"Review: {base_title}",
                description=(
                    "Review the workspace after fixes. "
                    "Check that all issues are resolved."
                ),
                role="reviewer",
                depends_on_parent=False,
                depends_on_siblings=True,
                chain_id=chain_id,
                target_repo=target_repo,
            )
        )

        return spawns


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------


def _build_fix_description(
    feedback: str,
    issues: list,
    output: dict,
) -> str:
    """Build a fix description from the reviewer's structured output.

    Includes numbered issues with severity, location, problem
    description, and suggested fix so the coder knows exactly
    what to address.
    """
    parts = ["Address the reviewer's feedback and fix the following issues:\n"]

    if issues:
        for i, issue in enumerate(issues, 1):
            if isinstance(issue, dict):
                sev = issue.get("severity", "major").upper()
                fp = issue.get("file", "")
                line = issue.get("line")
                loc = f" in `{fp}`" if fp else ""
                if line:
                    loc += f" (line {line})"
                parts.append(f"### Issue {i} [{sev}]{loc}")
                if issue.get("description"):
                    parts.append(f"**Problem:** {issue['description']}")
                if issue.get("suggestion"):
                    parts.append(f"**Fix:** {issue['suggestion']}")
                parts.append("")
            else:
                parts.append(f"- {str(issue)}")

    summary = output.get("summary", "")
    if summary:
        parts.append(f"\n## Reviewer Summary\n{summary}")

    if feedback and not issues:
        parts.append(f"\n## Reviewer Feedback\n{feedback[:3000]}")

    return "\n".join(parts)


def _build_fix_description_for_file(
    file_key: str,
    issues: list[dict],
    parent_job: dict,
) -> str:
    """Build a focused fix description for issues in a single file.

    Used when the reviewer finds issues across multiple files and we
    create parallel per-file fix coders.
    """
    if file_key == "_general":
        display = "General issues"
    else:
        display = file_key

    parts = [f"Fix the following issues in **{display}**:\n"]

    for issue in issues:
        if isinstance(issue, dict):
            sev = issue.get("severity", "major").upper()
            desc = issue.get("description", "")
            suggestion = issue.get("suggestion", "")
            line = issue.get("line")
            loc = f" (line {line})" if line else ""
            parts.append(f"- **[{sev}]{loc}** {desc}")
            if suggestion:
                parts.append(f"  **Fix:** {suggestion}")

    return "\n".join(parts)
