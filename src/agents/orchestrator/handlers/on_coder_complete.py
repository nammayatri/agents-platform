"""Completion handler — coder role.

After a coder subtask completes, capture the git diff and create a
reviewer subtask (or retry the coder if no changes were written).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from agents.orchestrator.handlers._base import HandlerContext
from agents.orchestrator.handlers._shared import propagate_dependencies

logger = logging.getLogger(__name__)


async def handle_coder_completion(
    ctx: HandlerContext,
    sub_task: dict,
    provider,
    workspace_path: str | None,
) -> None:
    """Coder completed -> create a reviewer subtask with diff context."""
    chain_id = sub_task.get("review_chain_id") or sub_task["id"]

    target_repo_json = sub_task.get("target_repo")
    if isinstance(target_repo_json, str):
        target_repo_json = json.loads(target_repo_json)

    # ── handle ──
    # Build the reviewer description: capture git diff, coder output, etc.

    desc_parts = [
        f"Review the changes from sub-task '{sub_task['title']}'.",
        "Check for bugs, security issues, code quality, and adherence to requirements.",
    ]

    coder_output = sub_task.get("output_result") or {}
    if isinstance(coder_output, dict):
        approach = coder_output.get("approach", "")
        if approach:
            desc_parts.append(f"\n## Implementation Approach\n{approach}")
        files_changed = coder_output.get("files_changed", [])
        if files_changed:
            desc_parts.append("\n## Files Changed\n" + "\n".join(f"- {f}" for f in files_changed))

    # Capture git diff from workspace
    has_diff = False
    if workspace_path:
        try:
            repo_dir = os.path.join(workspace_path, "repo")
            if not os.path.isdir(repo_dir):
                repo_dir = workspace_path

            async def _git(args):
                proc = await asyncio.create_subprocess_exec(
                    "git", *args, cwd=repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await proc.communicate()
                return out.decode(errors="replace").strip()

            diff_stat = await _git(["diff", "--stat", "HEAD"])
            diff_full = await _git(["diff", "HEAD"])

            if not diff_full:
                diff_stat = await _git(["diff", "--stat", "HEAD~1", "HEAD"])
                diff_full = await _git(["diff", "HEAD~1", "HEAD"])

            has_diff = bool(diff_stat or diff_full)

            if diff_stat:
                desc_parts.append(f"\n## Git Diff Summary\n```\n{diff_stat}\n```")
            if diff_full:
                if len(diff_full) > 10_000:
                    diff_full = diff_full[:10_000] + "\n... (truncated, use git diff to see full changes)"
                desc_parts.append(f"\n## Full Diff\n```diff\n{diff_full}\n```")
        except Exception:
            logger.warning("[%s] Failed to capture git diff for reviewer", ctx.todo_id, exc_info=True)

    # Fail coder if no changes on disk — don't create a reviewer for empty work
    has_files_from_output = bool(
        coder_output.get("files_changed") if isinstance(coder_output, dict) else False
    )
    if workspace_path and not has_diff and not has_files_from_output:
        logger.warning(
            "[%s] No changes on disk for coder %s in chain %s — failing coder",
            ctx.todo_id, sub_task["id"], chain_id,
        )
        await ctx.db.execute(
            "UPDATE sub_tasks SET status = 'failed', output_result = $2 WHERE id = $1",
            sub_task["id"],
            json.dumps({"error": "No changes written to disk. The coder described changes but did not write any files."}),
        )
        await ctx.post_system_message(
            f"**Review loop:** Coder sub-task '{sub_task['title']}' failed — no changes found on disk. "
            f"Creating retry coder."
        )
        # Create a retry coder with explicit instructions to write files
        retry_desc = (
            f"RETRY: The previous coder for '{sub_task['title']}' described changes but did not "
            "write any files to disk. You MUST use the write_file tool to actually create or "
            "modify files. After writing, verify your changes exist by using read_file.\n\n"
            f"Original task:\n{sub_task.get('description', '')}"
        )
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
            f"Retry: {sub_task['title']}",
            retry_desc,
            (sub_task.get("execution_order") or 0) + 1,
            [str(sub_task["id"])],
            chain_id,
            target_repo_json,
        )
        retry_id = str(row["id"])
        logger.info("Created retry coder %s for chain %s (no disk changes)", retry_id, chain_id)
        await propagate_dependencies(ctx, str(sub_task["id"]), [retry_id])
        return

    # ── next_action ──
    # Build review instructions and INSERT reviewer subtask

    desc_parts.append(
        "\n## Instructions\n"
        "IMPORTANT: First verify that actual code changes exist. If the diff above is "
        "empty or missing, you MUST reject with verdict 'needs_changes' and note that "
        "no files were written to disk.\n\n"
        "Review the diff above carefully. For each issue found, specify the exact "
        "file path and line number.\n\n"
        "You MUST output a JSON verdict at the end of your response:\n"
        '{"verdict": "approved"} or {"verdict": "needs_changes", "issues": ['
        '{"severity": "major", "file": "path/to/file.py", "line": 42, '
        '"description": "what is wrong", "suggestion": "how to fix it"}]}'
    )

    description = "\n".join(desc_parts)

    row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, review_chain_id, target_repo
        )
        VALUES ($1, $2, $3, 'reviewer', $4, $5, $6, $7)
        RETURNING id
        """,
        ctx.todo_id,
        f"Review: {sub_task['title']}",
        description,
        (sub_task.get("execution_order") or 0) + 1,
        [str(sub_task["id"])],
        chain_id,
        target_repo_json,
    )
    reviewer_id = str(row["id"])
    logger.info(
        "Created reviewer sub-task %s for chain %s (depends on coder %s)",
        reviewer_id, chain_id, sub_task["id"],
    )
    # Propagate: tasks depending on the coder should also wait for the reviewer
    await propagate_dependencies(ctx, str(sub_task["id"]), [reviewer_id])
    await ctx.post_system_message(
        f"**Review loop:** Created reviewer sub-task for '{sub_task['title']}'"
    )
