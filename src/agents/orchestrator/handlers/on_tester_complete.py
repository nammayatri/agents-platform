"""Completion handler — tester role.

After a tester subtask completes, parse pass/fail results and create
fix coder subtasks if there are failures.
"""

from __future__ import annotations

import logging

from agents.orchestrator.handlers._base import HandlerContext
from agents.orchestrator.handlers._shared import create_guardrail_subtask
from agents.utils.json_helpers import parse_llm_json

logger = logging.getLogger(__name__)

_MAX_TEST_FIX_ROUNDS = 3  # Max tester -> fix -> retest cycles


async def handle_tester_completion(
    ctx: HandlerContext,
    sub_task: dict,
    provider,
    workspace_path: str | None,
) -> None:
    """Tester completed -> pass through or create fix subtasks."""
    chain_id = sub_task.get("review_chain_id") or sub_task["id"]

    # ── validate ──
    # Parse output_result for passed/failures
    output = sub_task.get("output_result") or {}
    tester_passed = True
    tester_failures: list = []

    if isinstance(output, dict):
        tester_passed = output.get("passed", True)
        tester_failures = output.get("failures", [])
        if tester_passed and not tester_failures:
            content = output.get("content", "") or output.get("raw_content", "")
            parsed = parse_llm_json(content) if content else None
            if parsed and isinstance(parsed, dict):
                tester_passed = parsed.get("passed", True)
                tester_failures = parsed.get("failures", [])

    # ── handle (passed) ──
    if tester_passed or not tester_failures:
        logger.info(
            "[%s] Tester subtask %s passed — reviewer can proceed",
            ctx.todo_id, sub_task["id"],
        )
        return

    # ── next_action (failures) ──
    logger.info(
        "[%s] Tester subtask %s found %d failures — creating fix subtasks",
        ctx.todo_id, sub_task["id"], len(tester_failures),
    )
    summary = output.get("summary", "") if isinstance(output, dict) else ""
    await _create_test_fix_subtasks_from_tester(
        ctx, sub_task, chain_id, tester_failures, summary,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


async def _create_test_fix_subtasks_from_tester(
    ctx: HandlerContext,
    tester_st: dict,
    chain_id: str,
    failures: list[dict],
    summary: str,
) -> None:
    """Create structured coder fix subtasks from a tester subtask's failure output."""
    fix_rounds = await ctx.db.fetchval(
        "SELECT COUNT(*) FROM sub_tasks "
        "WHERE todo_id = $1 AND review_chain_id = $2 AND agent_role = 'tester'",
        ctx.todo_id, chain_id,
    ) or 0
    if fix_rounds >= _MAX_TEST_FIX_ROUNDS:
        logger.warning(
            "[%s] Test-fix loop capped at %d rounds for chain %s — letting reviewer proceed",
            ctx.todo_id, _MAX_TEST_FIX_ROUNDS, chain_id,
        )
        await ctx.post_system_message(
            f"**Test-fix loop capped at {_MAX_TEST_FIX_ROUNDS} rounds.** "
            "Proceeding to review with known test failures."
        )
        return

    # Group failures by type
    by_type: dict[str, list[dict]] = {}
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

    tester_id = str(tester_st["id"])
    fix_ids: list[str] = []

    for ftype, type_failures in by_type.items():
        label = type_labels.get(ftype, ftype)

        error_lines = []
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

        fix_id = await create_guardrail_subtask(
            ctx,
            title=f"Fix {label.lower()}",
            description=fix_desc,
            role="coder",
            depends_on=[tester_id],
            review_chain_id=chain_id,
        )
        fix_ids.append(fix_id)

    # Create a new tester subtask that depends on all fixes
    retest_desc = (
        "Re-run all build, typecheck, lint, and test commands to verify "
        "the fixes resolved the previously reported failures.\n\n"
        f"Previous failures summary: {summary[:500]}\n\n"
        "Run ALL checks and report structured results."
    )
    await create_guardrail_subtask(
        ctx,
        title="Re-test after fixes",
        description=retest_desc,
        role="tester",
        depends_on=fix_ids,
        review_loop=True,
        review_chain_id=chain_id,
    )

    # Block existing reviewer by adding fix subtasks as dependencies
    reviewer = await ctx.db.fetchrow(
        "SELECT id, depends_on FROM sub_tasks "
        "WHERE todo_id = $1 AND review_chain_id = $2 AND agent_role = 'reviewer' "
        "AND status = 'pending' LIMIT 1",
        ctx.todo_id, chain_id,
    )
    if reviewer:
        existing_deps = reviewer.get("depends_on") or []
        new_deps = list(set(existing_deps + fix_ids))
        await ctx.db.execute(
            "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
            reviewer["id"], new_deps,
        )
        logger.info(
            "[%s] Updated reviewer %s deps to include fix subtasks: %s",
            ctx.todo_id, reviewer["id"], fix_ids,
        )

    type_summary = ", ".join(
        f"{len(fs)} {type_labels.get(ft, ft).lower()}"
        for ft, fs in by_type.items()
    )
    await ctx.post_system_message(
        f"**Tester found failures:** {type_summary}.\n\n"
        f"Creating {len(fix_ids)} fix subtask(s) + re-test."
    )
