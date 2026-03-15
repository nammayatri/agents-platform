"""Completion handler — reviewer role.

After a reviewer subtask completes, either create a PR (approved) or
create fix subtasks (needs_changes).
"""

from __future__ import annotations

import json
import logging

from agents.orchestrator.handlers._base import HandlerContext
from agents.orchestrator.handlers._shared import (
    build_fix_description_for_file,
    create_pr_creator_subtask,
    extract_review_verdict,
    propagate_dependencies,
)

logger = logging.getLogger(__name__)


async def handle_reviewer_completion(
    ctx: HandlerContext,
    sub_task: dict,
    provider,
    workspace_path: str | None,
) -> None:
    """Reviewer completed -> create PR (approved) or fix subtasks (needs_changes)."""
    chain_id = sub_task.get("review_chain_id") or sub_task["id"]

    # ── validate ──
    # Extract verdict from output_result or content
    verdict = sub_task.get("review_verdict")
    if not verdict:
        output = sub_task.get("output_result") or {}
        if isinstance(output, dict):
            verdict = output.get("verdict")
        if not verdict:
            verdict = extract_review_verdict(
                output.get("content", "") if isinstance(output, dict) else str(output)
            )
        await ctx.db.execute(
            "UPDATE sub_tasks SET review_verdict = $2 WHERE id = $1",
            sub_task["id"], verdict,
        )

    # ── handle (approved) ──
    if verdict == "approved":
        output = sub_task.get("output_result") or {}
        await ctx.post_system_message(
            "**Code review: Approved.** Creating PR sub-task...",
            metadata={
                "action": "code_review_verdict",
                "task_id": ctx.todo_id,
                "subtask_title": sub_task.get("title", ""),
                "verdict": "approved",
                "feedback": output.get("content", "")[:500] if isinstance(output, dict) else "",
                "summary": output.get("summary", "") if isinstance(output, dict) else "",
            },
        )
        await create_pr_creator_subtask(ctx, sub_task, chain_id)
        return

    # ── handle (needs_changes) ──
    output = sub_task.get("output_result") or {}
    if isinstance(output, dict):
        reviewer_feedback = output.get("content", "")
        structured_issues = output.get("issues", [])
        review_summary = output.get("summary", "")
    else:
        reviewer_feedback = str(output)
        structured_issues = []
        review_summary = ""

    # Post review feedback to chat before creating fix subtasks
    issues_text = ""
    if structured_issues:
        parts = []
        for iss in structured_issues[:20]:
            if isinstance(iss, dict):
                sev = iss.get("severity", "major").upper()
                f = iss.get("file", "")
                loc = f" `{f}`" if f else ""
                if iss.get("line"):
                    loc += f":{iss['line']}"
                parts.append(f"- **[{sev}]**{loc} {iss.get('description', '')}")
        issues_text = "\n".join(parts)

    await ctx.post_system_message(
        f"**Code review: Changes requested**\n\n"
        + (f"{review_summary}\n\n" if review_summary else "")
        + (issues_text if issues_text else reviewer_feedback[:1500]),
        metadata={
            "action": "code_review_verdict",
            "task_id": ctx.todo_id,
            "subtask_title": sub_task.get("title", ""),
            "verdict": "needs_changes",
            "feedback": reviewer_feedback[:2000],
            "issues": structured_issues[:20],
            "summary": review_summary,
        },
    )

    await _create_fix_subtasks(
        ctx, sub_task, chain_id, reviewer_feedback, structured_issues,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


async def _create_fix_subtasks(
    ctx: HandlerContext,
    reviewer_st: dict,
    chain_id: str,
    feedback: str,
    structured_issues: list | None = None,
) -> None:
    """Create fix sub-tasks from reviewer feedback.

    If issues span multiple files, creates parallel per-file fix
    sub-tasks plus a pre-created reviewer. Otherwise single fix sub-task.
    """
    # Group issues by file
    file_groups: dict[str, list[dict]] = {}
    if structured_issues:
        for issue in structured_issues:
            if isinstance(issue, dict):
                key = issue.get("file") or "_general"
                file_groups.setdefault(key, []).append(issue)

    # Single-file or no structured issues -> legacy single fix sub-task
    if len(file_groups) <= 1:
        await _create_single_fix_subtask(
            ctx, reviewer_st, chain_id, feedback, structured_issues,
        )
        return

    # Multi-file -> parallel fix sub-tasks + pre-created reviewer
    target_repo_json = reviewer_st.get("target_repo")
    if isinstance(target_repo_json, str):
        target_repo_json = json.loads(target_repo_json)

    base_order = (reviewer_st.get("execution_order") or 0) + 1
    base_title = reviewer_st["title"].removeprefix("Review: ")
    fix_task_ids: list[str] = []

    for file_key, file_issues in file_groups.items():
        description = build_fix_description_for_file(
            file_key, file_issues, reviewer_st,
        )
        display_file = file_key if file_key != "_general" else "general"
        row = await ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on,
                review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'coder', $4, $5, FALSE, $6, $7)
            RETURNING id
            """,
            ctx.todo_id,
            f"Fix ({display_file}): {base_title}",
            description,
            base_order,
            [str(reviewer_st["id"])],
            chain_id,
            target_repo_json,
        )
        fix_task_ids.append(str(row["id"]))
        logger.info(
            "Created parallel fix sub-task %s for file %s in chain %s",
            row["id"], file_key, chain_id,
        )

    # Pre-create a reviewer that depends on all parallel fix tasks
    reviewer_row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on,
            review_loop, review_chain_id, target_repo
        )
        VALUES ($1, $2, $3, 'reviewer', $4, $5, FALSE, $6, $7)
        RETURNING id
        """,
        ctx.todo_id,
        f"Review: {base_title}",
        "Review the workspace after parallel fixes. Check that all issues are resolved.",
        base_order + 1,
        fix_task_ids,
        chain_id,
        target_repo_json,
    )
    new_reviewer_id = str(reviewer_row["id"])
    logger.info(
        "Created pre-reviewer sub-task %s (depends on %d fixes) for chain %s",
        new_reviewer_id, len(fix_task_ids), chain_id,
    )
    # Propagate: tasks depending on the old reviewer should also wait
    # for the new reviewer (the final output of this fix cycle)
    await propagate_dependencies(ctx, str(reviewer_st["id"]), [new_reviewer_id])
    await ctx.post_system_message(
        f"**Review loop:** Reviewer requested changes across {len(file_groups)} files. "
        f"Created {len(fix_task_ids)} parallel fix sub-tasks + reviewer."
    )


async def _create_single_fix_subtask(
    ctx: HandlerContext,
    reviewer_st: dict,
    chain_id: str,
    feedback: str,
    structured_issues: list | None = None,
) -> None:
    """Create a single coder fix sub-task (legacy path)."""
    target_repo_json = reviewer_st.get("target_repo")
    if isinstance(target_repo_json, str):
        target_repo_json = json.loads(target_repo_json)

    desc_parts = ["Address the reviewer's feedback and fix the following issues:\n"]

    if structured_issues:
        for i, issue in enumerate(structured_issues, 1):
            if isinstance(issue, dict):
                severity = issue.get("severity", "major").upper()
                file_path = issue.get("file", "")
                line = issue.get("line")
                issue_desc = issue.get("description", "")
                suggestion = issue.get("suggestion", "")

                location = ""
                if file_path:
                    location = f" in `{file_path}`"
                    if line:
                        location += f" (line {line})"

                desc_parts.append(f"### Issue {i} [{severity}]{location}")
                if issue_desc:
                    desc_parts.append(f"**Problem:** {issue_desc}")
                if suggestion:
                    desc_parts.append(f"**Fix:** {suggestion}")
                desc_parts.append("")
            else:
                desc_parts.append(f"- {str(issue)}")

    reviewer_output = reviewer_st.get("output_result") or {}
    if isinstance(reviewer_output, dict):
        summary = reviewer_output.get("summary", "")
        if summary:
            desc_parts.append(f"\n## Reviewer Summary\n{summary}")

    if feedback and not structured_issues:
        desc_parts.append(f"\n## Reviewer Feedback\n{feedback[:3000]}")

    description = "\n".join(desc_parts)

    row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, review_loop, review_chain_id, target_repo
        )
        VALUES ($1, $2, $3, 'coder', $4, $5, TRUE, $6, $7)
        RETURNING id
        """,
        ctx.todo_id,
        f"Fix: {reviewer_st['title'].removeprefix('Review: ')}",
        description,
        (reviewer_st.get("execution_order") or 0) + 1,
        [str(reviewer_st["id"])],
        chain_id,
        target_repo_json,
    )
    fix_id = str(row["id"])
    logger.info("Created fix sub-task %s for chain %s", fix_id, chain_id)
    # Propagate: tasks depending on the old reviewer should also wait for the fix
    await propagate_dependencies(ctx, str(reviewer_st["id"]), [fix_id])
    await ctx.post_system_message(
        "**Review loop:** Reviewer requested changes. Created fix sub-task."
    )
