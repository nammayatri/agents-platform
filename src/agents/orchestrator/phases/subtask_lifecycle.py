"""Subtask lifecycle — review-merge loop, role-specific handlers, guardrails.

Shared cross-phase methods for creating follow-up subtasks (reviewer,
fix, merge, PR, release) and executing role-specific procedural handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING

from agents.config.settings import settings
from agents.utils.json_helpers import parse_llm_json

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 5
_MAX_TEST_FIX_ROUNDS = 3  # Max tester→fix→retest cycles


class SubtaskLifecycle:
    """Review-merge loop, role-specific handlers, and guardrail subtask creation."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    # ------------------------------------------------------------------
    # Git provider resolution for subtasks (handles target_repo for deps)
    # ------------------------------------------------------------------

    async def _resolve_git_for_subtask(
        self, sub_task: dict, project: dict,
    ) -> tuple:
        """Resolve git provider, owner, and repo for a subtask.

        If the subtask has a target_repo (dependency), resolves from that.
        Otherwise falls back to the main project repo.

        Returns (git_provider_instance, owner, repo).
        """
        from agents.orchestrator.git_providers.factory import (
            create_git_provider,
            parse_repo_url,
        )
        from agents.infra.crypto import decrypt

        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo)

        if target_repo and target_repo.get("repo_url"):
            repo_url = target_repo["repo_url"]
            git_provider_id = target_repo.get("git_provider_id")
        else:
            repo_url = project.get("repo_url")
            git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None

        if not repo_url:
            raise ValueError("No repo_url available for subtask")

        token = None
        provider_type = None
        api_base_url = None
        if git_provider_id:
            gp_row = await self._coord.db.fetchrow(
                "SELECT provider_type, api_base_url, token_enc "
                "FROM git_provider_configs WHERE id = $1",
                git_provider_id,
            )
            if gp_row:
                token = decrypt(gp_row["token_enc"]) if gp_row.get("token_enc") else None
                provider_type = gp_row["provider_type"]
                api_base_url = gp_row.get("api_base_url")

        git = create_git_provider(
            provider_type=provider_type,
            api_base_url=api_base_url,
            token=token,
            repo_url=repo_url,
        )
        owner, repo_name = parse_repo_url(repo_url)
        return git, owner, repo_name

    # ------------------------------------------------------------------
    # Subtask completion handler (entry point of the review-merge loop)
    # ------------------------------------------------------------------

    async def handle_subtask_completion(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """After a sub-task completes, check if it's part of a review loop
        and create follow-up sub-tasks (reviewer, fix, merge_agent) dynamically."""
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

        # Safety: count sub-tasks in this chain
        chain_count = await coord.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks WHERE review_chain_id = $1",
            chain_id,
        )
        if chain_count >= MAX_REVIEW_ROUNDS * 2:
            logger.warning(
                "Review chain %s hit max rounds (%d sub-tasks), stopping",
                chain_id, chain_count,
            )
            await coord._post_system_message(
                f"**Review chain capped at {MAX_REVIEW_ROUNDS} rounds.** "
                "Creating PR sub-task with current state."
            )
            await self.create_pr_creator_subtask(st, chain_id)
            return

        if role == "coder":
            await self.create_reviewer_subtask(st, chain_id, workspace_path)

        elif role == "tester":
            output = st.get("output_result") or {}
            tester_passed = True
            tester_failures = []

            if isinstance(output, dict):
                tester_passed = output.get("passed", True)
                tester_failures = output.get("failures", [])
                if tester_passed and not tester_failures:
                    content = output.get("content", "") or output.get("raw_content", "")
                    parsed = parse_llm_json(content) if content else None
                    if parsed and isinstance(parsed, dict):
                        tester_passed = parsed.get("passed", True)
                        tester_failures = parsed.get("failures", [])

            if tester_passed or not tester_failures:
                logger.info(
                    "[%s] Tester subtask %s passed — reviewer can proceed",
                    coord.todo_id, st["id"],
                )
            else:
                logger.info(
                    "[%s] Tester subtask %s found %d failures — creating fix subtasks",
                    coord.todo_id, st["id"], len(tester_failures),
                )
                summary = output.get("summary", "") if isinstance(output, dict) else ""
                await self.create_test_fix_subtasks_from_tester(
                    st, chain_id, tester_failures, summary,
                )

        elif role == "reviewer":
            verdict = st.get("review_verdict")
            if not verdict:
                output = st.get("output_result") or {}
                if isinstance(output, dict):
                    verdict = output.get("verdict")
                if not verdict:
                    verdict = self.extract_review_verdict(
                        output.get("content", "") if isinstance(output, dict) else str(output)
                    )
                await coord.db.execute(
                    "UPDATE sub_tasks SET review_verdict = $2 WHERE id = $1",
                    st["id"], verdict,
                )

            if verdict == "approved":
                output = st.get("output_result") or {}
                await coord._post_system_message(
                    "**Code review: Approved.** Creating PR sub-task...",
                    metadata={
                        "action": "code_review_verdict",
                        "task_id": coord.todo_id,
                        "subtask_title": st.get("title", ""),
                        "verdict": "approved",
                        "feedback": output.get("content", "")[:500] if isinstance(output, dict) else "",
                        "summary": output.get("summary", "") if isinstance(output, dict) else "",
                    },
                )
                await self.create_pr_creator_subtask(st, chain_id)
            else:
                output = st.get("output_result") or {}
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

                await coord._post_system_message(
                    f"**Code review: Changes requested**\n\n"
                    + (f"{review_summary}\n\n" if review_summary else "")
                    + (issues_text if issues_text else reviewer_feedback[:1500]),
                    metadata={
                        "action": "code_review_verdict",
                        "task_id": coord.todo_id,
                        "subtask_title": st.get("title", ""),
                        "verdict": "needs_changes",
                        "feedback": reviewer_feedback[:2000],
                        "issues": structured_issues[:20],
                        "summary": review_summary,
                    },
                )

                await self.create_fix_subtasks(
                    st, chain_id, reviewer_feedback, structured_issues,
                )

    # ------------------------------------------------------------------
    # Dependency propagation
    # ------------------------------------------------------------------

    async def _propagate_dependencies(
        self, parent_subtask_id: str, new_subtask_ids: list[str],
    ) -> None:
        """When a subtask creates continuation subtasks, propagate to dependents.

        Any subtask that depends on `parent_subtask_id` should also depend on
        the newly created subtask IDs so it waits for the full chain to finish.
        """
        if not new_subtask_ids:
            return
        coord = self._coord

        # Find all subtasks that have parent_subtask_id in their depends_on
        dependents = await coord.db.fetch(
            "SELECT id, depends_on FROM sub_tasks "
            "WHERE todo_id = $1 AND $2 = ANY(depends_on) AND status = 'pending'",
            coord.todo_id, parent_subtask_id,
        )
        if not dependents:
            return

        for dep in dependents:
            dep_id = str(dep["id"])
            # Skip if the dependent IS one of the new subtasks (avoid circular deps)
            if dep_id in new_subtask_ids:
                continue
            existing = [str(d) for d in (dep["depends_on"] or [])]
            to_add = [sid for sid in new_subtask_ids if sid not in existing and sid != dep_id]
            if not to_add:
                continue
            updated_deps = existing + to_add
            await coord.db.execute(
                "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                dep["id"], updated_deps,
            )
            logger.info(
                "[%s] Propagated deps: subtask %s now also depends on %s (via parent %s)",
                coord.todo_id, dep_id[:8], [s[:8] for s in to_add], parent_subtask_id[:8],
            )

    # ------------------------------------------------------------------
    # Reviewer / fix subtask creation
    # ------------------------------------------------------------------

    async def create_reviewer_subtask(
        self, coder_st: dict, chain_id, workspace_path: str | None = None,
    ) -> None:
        """Create a reviewer sub-task that depends on the completed coder sub-task."""
        coord = self._coord
        target_repo_json = coder_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        desc_parts = [
            f"Review the changes from sub-task '{coder_st['title']}'.",
            "Check for bugs, security issues, code quality, and adherence to requirements.",
        ]

        coder_output = coder_st.get("output_result") or {}
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
                logger.warning("[%s] Failed to capture git diff for reviewer", coord.todo_id, exc_info=True)

        # Fail coder if no changes on disk — don't create a reviewer for empty work
        has_files_from_output = bool(
            coder_output.get("files_changed") if isinstance(coder_output, dict) else False
        )
        if workspace_path and not has_diff and not has_files_from_output:
            logger.warning(
                "[%s] No changes on disk for coder %s in chain %s — failing coder",
                coord.todo_id, coder_st["id"], chain_id,
            )
            await coord.db.execute(
                "UPDATE sub_tasks SET status = 'failed', output_result = $2 WHERE id = $1",
                coder_st["id"],
                json.dumps({"error": "No changes written to disk. The coder described changes but did not write any files."}),
            )
            await coord._post_system_message(
                f"**Review loop:** Coder sub-task '{coder_st['title']}' failed — no changes found on disk. "
                f"Creating retry coder."
            )
            # Create a retry coder with explicit instructions to write files
            retry_desc = (
                f"RETRY: The previous coder for '{coder_st['title']}' described changes but did not "
                "write any files to disk. You MUST use the write_file tool to actually create or "
                "modify files. After writing, verify your changes exist by using read_file.\n\n"
                f"Original task:\n{coder_st.get('description', '')}"
            )
            row = await coord.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, review_loop, review_chain_id, target_repo
                )
                VALUES ($1, $2, $3, 'coder', $4, $5, TRUE, $6, $7)
                RETURNING id
                """,
                coord.todo_id,
                f"Retry: {coder_st['title']}",
                retry_desc,
                (coder_st.get("execution_order") or 0) + 1,
                [str(coder_st["id"])],
                chain_id,
                target_repo_json,
            )
            retry_id = str(row["id"])
            logger.info("Created retry coder %s for chain %s (no disk changes)", retry_id, chain_id)
            await self._propagate_dependencies(str(coder_st["id"]), [retry_id])
            return

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

        row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'reviewer', $4, $5, $6, $7)
            RETURNING id
            """,
            coord.todo_id,
            f"Review: {coder_st['title']}",
            description,
            (coder_st.get("execution_order") or 0) + 1,
            [str(coder_st["id"])],
            chain_id,
            target_repo_json,
        )
        reviewer_id = str(row["id"])
        logger.info(
            "Created reviewer sub-task %s for chain %s (depends on coder %s)",
            reviewer_id, chain_id, coder_st["id"],
        )
        # Propagate: tasks depending on the coder should also wait for the reviewer
        await self._propagate_dependencies(str(coder_st["id"]), [reviewer_id])
        await coord._post_system_message(
            f"**Review loop:** Created reviewer sub-task for '{coder_st['title']}'"
        )

    async def create_fix_subtasks(
        self, reviewer_st: dict, chain_id, feedback: str,
        structured_issues: list | None = None,
    ) -> None:
        """Create fix sub-tasks from reviewer feedback.

        If issues span multiple files, creates parallel per-file fix
        sub-tasks plus a pre-created reviewer. Otherwise single fix sub-task.
        """
        coord = self._coord
        # Group issues by file
        file_groups: dict[str, list[dict]] = {}
        if structured_issues:
            for issue in structured_issues:
                if isinstance(issue, dict):
                    key = issue.get("file") or "_general"
                    file_groups.setdefault(key, []).append(issue)

        # Single-file or no structured issues → legacy single fix sub-task
        if len(file_groups) <= 1:
            await self.create_single_fix_subtask(
                reviewer_st, chain_id, feedback, structured_issues,
            )
            return

        # Multi-file → parallel fix sub-tasks + pre-created reviewer
        target_repo_json = reviewer_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        base_order = (reviewer_st.get("execution_order") or 0) + 1
        base_title = reviewer_st["title"].removeprefix("Review: ")
        fix_task_ids: list[str] = []

        for file_key, file_issues in file_groups.items():
            description = self.build_fix_description_for_file(
                file_key, file_issues, reviewer_st,
            )
            display_file = file_key if file_key != "_general" else "general"
            row = await coord.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on,
                    review_loop, review_chain_id, target_repo
                )
                VALUES ($1, $2, $3, 'coder', $4, $5, FALSE, $6, $7)
                RETURNING id
                """,
                coord.todo_id,
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
        reviewer_row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on,
                review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'reviewer', $4, $5, FALSE, $6, $7)
            RETURNING id
            """,
            coord.todo_id,
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
        await self._propagate_dependencies(
            str(reviewer_st["id"]), [new_reviewer_id],
        )
        await coord._post_system_message(
            f"**Review loop:** Reviewer requested changes across {len(file_groups)} files. "
            f"Created {len(fix_task_ids)} parallel fix sub-tasks + reviewer."
        )

    async def create_single_fix_subtask(
        self, reviewer_st: dict, chain_id, feedback: str,
        structured_issues: list | None = None,
    ) -> None:
        """Create a single coder fix sub-task (legacy path)."""
        coord = self._coord
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

        row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_loop, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'coder', $4, $5, TRUE, $6, $7)
            RETURNING id
            """,
            coord.todo_id,
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
        await self._propagate_dependencies(str(reviewer_st["id"]), [fix_id])
        await coord._post_system_message(
            "**Review loop:** Reviewer requested changes. Created fix sub-task."
        )

    @staticmethod
    def build_fix_description_for_file(
        file_key: str, issues: list[dict], reviewer_st: dict,
    ) -> str:
        """Build a focused fix description for issues in a single file."""
        if file_key == "_general":
            parts = ["Fix the following general issues:\n"]
        else:
            parts = [f"Fix the following issues in `{file_key}`:\n"]

        for i, issue in enumerate(issues, 1):
            severity = issue.get("severity", "major").upper()
            line = issue.get("line")
            issue_desc = issue.get("description", "")
            suggestion = issue.get("suggestion", "")

            line_ref = f" (line {line})" if line else ""
            parts.append(f"### Issue {i} [{severity}]{line_ref}")
            if issue_desc:
                parts.append(f"**Problem:** {issue_desc}")
            if suggestion:
                parts.append(f"**Fix:** {suggestion}")
            parts.append("")

        reviewer_output = reviewer_st.get("output_result") or {}
        if isinstance(reviewer_output, dict):
            summary = reviewer_output.get("summary", "")
            if summary:
                parts.append(f"\n## Reviewer Summary\n{summary}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Merge / PR subtask creation
    # ------------------------------------------------------------------

    async def _requires_merge_approval(self) -> bool:
        """Check project setting: should we skip auto-merge?"""
        coord = self._coord
        todo = await coord._load_todo()
        proj = await coord.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1",
            todo["project_id"],
        )
        settings = (proj or {}).get("settings_json") or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        return bool(settings.get("require_merge_approval", False))

    async def create_pr_creator_subtask(self, approved_st: dict, chain_id=None) -> None:
        """Create a pr_creator sub-task after reviewer approval."""
        coord = self._coord
        depends_on = []
        if chain_id:
            chain_tasks = await coord.db.fetch(
                "SELECT id FROM sub_tasks WHERE todo_id = $1 AND "
                "(review_chain_id = $2 OR id = $2)",
                coord.todo_id,
                chain_id,
            )
            depends_on = [str(t["id"]) for t in chain_tasks]

        approved_id = str(approved_st["id"])
        if approved_id not in depends_on:
            depends_on.append(approved_id)

        target_repo_json = approved_st.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_chain_id, target_repo
            )
            VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6, $7)
            RETURNING id
            """,
            coord.todo_id,
            "Create Pull Request",
            "Commit all workspace changes, push to a feature branch, and create a pull request.",
            (approved_st.get("execution_order") or 0) + 1,
            depends_on,
            chain_id,
            target_repo_json,
        )
        logger.info("Created pr_creator sub-task %s for chain %s", row["id"], chain_id)
        await coord._post_system_message(
            "**Review loop:** Reviewer approved. Created PR sub-task."
        )

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    async def ensure_coding_guardrails(self, workspace_path: str | None) -> bool:
        """Check for missing tester/reviewer subtasks on coding tasks.

        Returns True if new guardrail subtasks were created.
        """
        coord = self._coord
        coder_subtasks = await coord.db.fetch(
            "SELECT id, title, review_loop, review_chain_id FROM sub_tasks "
            "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
            coord.todo_id,
        )
        if not coder_subtasks:
            return False

        unreviewed_coders = [
            st for st in coder_subtasks
            if not st["review_loop"] and not st["review_chain_id"]
        ]
        if not unreviewed_coders:
            return False

        existing_roles = await coord.db.fetch(
            "SELECT DISTINCT agent_role FROM sub_tasks "
            "WHERE todo_id = $1 AND (review_chain_id IS NULL)",
            coord.todo_id,
        )
        existing = {r["agent_role"] for r in existing_roles}

        created: list[tuple[str, str]] = []
        coder_ids = [str(st["id"]) for st in unreviewed_coders]
        coder_titles = [st["title"] for st in unreviewed_coders]
        chain_id = coder_ids[0]

        # 1. Tester guardrail
        if "tester" not in existing:
            tester_desc = (
                "Validate the code changes made by the coder subtask(s):\n"
                + "\n".join(f"- {t}" for t in coder_titles)
                + "\n\nRun ALL build, typecheck, lint, and test commands. "
                "Write additional tests for new/changed functions if needed.\n\n"
                "You MUST output structured JSON via task_complete:\n"
                '```json\n'
                '{"passed": true/false, "summary": "...", "failures": [\n'
                '  {"file": "path", "type": "build|type_error|test|lint|runtime", "error": "message"}\n'
                ']}\n'
                '```'
            )
            tester_id = await self.create_guardrail_subtask(
                title="Test implemented changes",
                description=tester_desc,
                role="tester",
                depends_on=coder_ids,
                review_loop=True,
                review_chain_id=chain_id,
            )
            created.append(("tester", tester_id))

        # 2. Reviewer guardrail
        if "reviewer" not in existing:
            deps = list(coder_ids)
            if created:
                deps.append(created[-1][1])
            reviewer_desc = (
                "Review all code changes for quality, security, correctness, and adherence "
                "to requirements. Check the coder subtask(s):\n"
                + "\n".join(f"- {t}" for t in coder_titles)
                + "\n\nYou MUST output a JSON verdict at the end of your response:\n"
                '{"verdict": "approved"} or {"verdict": "needs_changes", "issues": ["issue1", ...]}'
            )
            reviewer_id = await self.create_guardrail_subtask(
                title="Review code changes",
                description=reviewer_desc,
                role="reviewer",
                depends_on=deps,
                review_loop=True,
                review_chain_id=chain_id,
            )
            created.append(("reviewer", reviewer_id))

        if created:
            # Propagate: tasks depending on coders should also wait for guardrails
            all_new_ids = [sid for _, sid in created]
            for coder_id in coder_ids:
                await self._propagate_dependencies(coder_id, all_new_ids)

            roles = ", ".join(r for r, _ in created)
            logger.info(
                "[%s] Coding guardrails: auto-created %s subtask(s) for unreviewed coder work",
                coord.todo_id, roles,
            )
            await coord._post_system_message(
                f"**Guardrail:** Auto-created {roles} subtask(s) to ensure code quality."
            )
            return True

        return False

    async def create_guardrail_subtask(
        self,
        title: str,
        description: str,
        role: str,
        depends_on: list[str],
        *,
        review_loop: bool = False,
        review_chain_id: str | None = None,
    ) -> str:
        """Create a guardrail subtask and return its ID as string."""
        coord = self._coord
        max_order = 0
        if depends_on:
            rows = await coord.db.fetch(
                "SELECT execution_order FROM sub_tasks WHERE id = ANY($1)",
                depends_on,
            )
            max_order = max((r["execution_order"] or 0) for r in rows) if rows else 0

        row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, review_loop, review_chain_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            coord.todo_id,
            title,
            description,
            role,
            max_order + 1,
            depends_on,
            review_loop,
            review_chain_id,
        )
        st_id = str(row["id"])
        logger.info(
            "[%s] Created guardrail subtask %s: role=%s title=%s depends_on=%s review_loop=%s chain=%s",
            coord.todo_id, st_id, role, title, depends_on, review_loop, review_chain_id,
        )
        return st_id

    # ------------------------------------------------------------------
    # Review verdict extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_review_verdict(content: str) -> str:
        """Parse reviewer verdict from output content."""
        data = parse_llm_json(content)
        if data is not None:
            verdict = data.get("verdict", "").lower()
            if verdict in ("approved", "needs_changes"):
                return verdict

        lower = content.lower()
        if "approved" in lower and "needs_changes" not in lower:
            return "approved"
        if "needs_changes" in lower or "needs changes" in lower or "request changes" in lower:
            return "needs_changes"

        return "needs_changes"

    # ------------------------------------------------------------------
    # Test-fix from tester subtask
    # ------------------------------------------------------------------

    async def create_test_fix_subtasks_from_tester(
        self,
        tester_st: dict,
        chain_id,
        failures: list[dict],
        summary: str,
    ) -> None:
        """Create structured coder fix subtasks from a tester subtask's failure output."""
        coord = self._coord

        fix_rounds = await coord.db.fetchval(
            "SELECT COUNT(*) FROM sub_tasks "
            "WHERE todo_id = $1 AND review_chain_id = $2 AND agent_role = 'tester'",
            coord.todo_id, chain_id,
        ) or 0
        if fix_rounds >= _MAX_TEST_FIX_ROUNDS:
            logger.warning(
                "[%s] Test-fix loop capped at %d rounds for chain %s — letting reviewer proceed",
                coord.todo_id, _MAX_TEST_FIX_ROUNDS, chain_id,
            )
            await coord._post_system_message(
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
        fix_ids = []

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

            fix_id = await self.create_guardrail_subtask(
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
        await self.create_guardrail_subtask(
            title="Re-test after fixes",
            description=retest_desc,
            role="tester",
            depends_on=fix_ids,
            review_loop=True,
            review_chain_id=chain_id,
        )

        # Block existing reviewer by adding fix subtasks as dependencies
        reviewer = await coord.db.fetchrow(
            "SELECT id, depends_on FROM sub_tasks "
            "WHERE todo_id = $1 AND review_chain_id = $2 AND agent_role = 'reviewer' "
            "AND status = 'pending' LIMIT 1",
            coord.todo_id, chain_id,
        )
        if reviewer:
            existing_deps = reviewer.get("depends_on") or []
            new_deps = list(set(existing_deps + fix_ids))
            await coord.db.execute(
                "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                reviewer["id"], new_deps,
            )
            logger.info(
                "[%s] Updated reviewer %s deps to include fix subtasks: %s",
                coord.todo_id, reviewer["id"], fix_ids,
            )

        type_summary = ", ".join(
            f"{len(fs)} {type_labels.get(ft, ft).lower()}"
            for ft, fs in by_type.items()
        )
        await coord._post_system_message(
            f"**Tester found failures:** {type_summary}.\n\n"
            f"Creating {len(fix_ids)} fix subtask(s) + re-test."
        )

    # ------------------------------------------------------------------
    # Pre-commit hook fix description builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pre_commit_fix_description(error_output: str) -> str:
        """Build a detailed coder subtask description for fixing pre-commit hook errors."""
        return (
            "The git pre-commit hooks failed when trying to commit your changes. "
            "You MUST fix every error and then successfully commit.\n\n"
            "## Workflow (repeat until commit succeeds)\n\n"
            "1. Read the pre-commit errors below.\n"
            "2. Read each failing file to understand context.\n"
            "3. Fix the errors.\n"
            "4. Stage and commit: `git add -A && git commit -m \"fix: resolve pre-commit hook errors\"`\n"
            "5. If the commit **fails again**, read the NEW error output, fix the new errors, "
            "and run the commit command again. **Keep looping until the commit succeeds.**\n"
            "6. Only call `task_complete` after a successful commit.\n\n"
            "## Critical Rules\n\n"
            "1. **Fix every error** — do NOT skip or ignore any.\n"
            "2. **Do NOT suppress checks** — no `eslint-disable`, `@ts-ignore`, `type: ignore`, "
            "`noqa`, `# noinspection`, `prettier-ignore`, or any other skip/suppress comment.\n"
            "3. **Do NOT revert the functional changes** — only fix the lint/type/style issues "
            "while preserving the intended behavior.\n"
            "4. **Do NOT remove or simplify existing code** just to avoid lint errors. "
            "Rewrite the code to satisfy the rule properly.\n"
            "5. **NEVER use `--no-verify`** — always let pre-commit hooks run.\n"
            "6. **You MUST commit successfully** — do not mark the task complete without a "
            "successful `git commit` (exit code 0).\n\n"
            "## Common Fix Patterns\n\n"
            "- **`functional/immutable-data`** (Modifying object/array/map not allowed): "
            "Use spread syntax (`{...obj, key: value}`), `.map()`, `.filter()`, "
            "`new Map([...oldMap, [key, value]])`, or `Object.assign({}, obj, updates)` "
            "instead of direct mutation.\n"
            "- **`no-as-in-modified-files`** (No type assertions with `as`): "
            "Use `satisfies`, generic type parameters, proper type annotations on variables, "
            "or type guard functions instead of `as` casts.\n"
            "- **`functional/no-let`** (No `let`): "
            "Use `const` with ternary expressions, `Array.reduce()`, or immediately-invoked "
            "functions to avoid mutable bindings.\n"
            "- **`no-console`** (No console.log): "
            "Remove `console.log` calls or replace with allowed methods "
            "(`console.warn`, `console.error`, `console.info`).\n"
            "- **`no-non-null-assertion`** (No `!` assertions): "
            "Use optional chaining (`?.`), nullish coalescing (`??`), "
            "or explicit null checks with early returns.\n"
            "- **`immutable-data` on arrays** (No `.push()`, `.splice()`, etc.): "
            "Use `[...arr, newItem]`, `.concat()`, or `.filter()` to produce new arrays.\n\n"
            "## Pre-Commit Errors\n\n"
            f"```\n{error_output[:6000]}\n```\n\n"
            "**Remember:** Fix → `git add -A && git commit` → if it fails, fix again → repeat. "
            "Do NOT call task_complete until the commit exits with code 0."
        )

    # ------------------------------------------------------------------
    # Workspace finalization (commit → push → PR)
    # ------------------------------------------------------------------

    async def finalize_subtask_workspace(
        self, sub_task: dict, workspace_path: str | None,
    ) -> dict | None:
        """Deterministic commit → push → PR for a sub-task's workspace changes."""
        coord = self._coord
        if not workspace_path:
            logger.warning("[%s] Cannot finalize: no workspace_path", coord.todo_id)
            await coord._post_system_message("**PR creation skipped:** no workspace path available.")
            return None

        try:
            todo = await coord._load_todo()
            short_id = str(coord.todo_id)[:8]

            target_repo = sub_task.get("target_repo")
            if isinstance(target_repo, str):
                target_repo = json.loads(target_repo)

            if target_repo and target_repo.get("repo_url"):
                dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
                branch_name = f"task/{short_id}-{dep_name}"
                base_branch = target_repo.get("default_branch") or "main"
            else:
                project = await coord.db.fetchrow(
                    "SELECT repo_url, default_branch FROM projects WHERE id = $1",
                    todo["project_id"],
                )
                if not project or not project.get("repo_url"):
                    logger.warning("[%s] Cannot finalize: no repo_url on project", coord.todo_id)
                    await coord._post_system_message("**PR creation skipped:** no repository URL configured on the project.")
                    return None
                branch_name = f"task/{short_id}"
                base_branch = project.get("default_branch") or "main"

            logger.info("[%s] Finalizing workspace: branch=%s base=%s dep=%s path=%s",
                        coord.todo_id, branch_name, base_branch,
                        bool(target_repo), workspace_path)

            # Step 1: commit and push
            commit_result = await coord.workspace_mgr.commit_and_push(
                workspace_path,
                message=f"[agents] {sub_task['title']}",
                branch=branch_name,
            )

            # If pre-commit hooks failed, signal the caller to create a fix subtask
            if commit_result.get("pre_commit_failed"):
                return {
                    "_pre_commit_failed": True,
                    "pre_commit_output": commit_result.get("pre_commit_output", ""),
                    "workspace_path": workspace_path,
                }

            if not commit_result["success"]:
                error = commit_result.get("error", "unknown error")
                logger.error("[%s] commit_and_push failed for branch %s: %s", coord.todo_id, branch_name, error)
                await coord._post_system_message(f"**PR creation failed:** could not push to branch `{branch_name}` — {error}")
                return None

            logger.info("[%s] Pushed to branch %s", coord.todo_id, branch_name)

            # Step 2: check if PR already exists for this branch
            existing_pr = await coord.db.fetchrow(
                "SELECT id, pr_number, pr_url FROM deliverables "
                "WHERE todo_id = $1 AND type = 'pull_request' AND branch_name = $2",
                coord.todo_id,
                branch_name,
            )
            if existing_pr:
                logger.info("[%s] PR already exists: #%s", coord.todo_id, existing_pr["pr_number"])
                return {"number": existing_pr["pr_number"], "url": existing_pr.get("pr_url")}

            # Step 3: create PR
            if target_repo and target_repo.get("repo_url"):
                pr_info = await coord.workspace_mgr.create_pr_for_repo(
                    repo_url=target_repo["repo_url"],
                    git_provider_id=target_repo.get("git_provider_id"),
                    head_branch=branch_name,
                    base_branch=base_branch,
                    title=todo["title"],
                    body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
                )
            else:
                pr_info = await coord.workspace_mgr.create_pr(
                    str(todo["project_id"]),
                    head_branch=branch_name,
                    base_branch=base_branch,
                    title=todo["title"],
                    body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
                )

            # Step 4: record deliverable
            dep_repo_name = None
            if target_repo and target_repo.get("name"):
                dep_repo_name = target_repo["name"]

            if pr_info:
                logger.info("[%s] PR created: %s", coord.todo_id, pr_info.get("url"))
                await coord.db.execute(
                    """
                    INSERT INTO deliverables (
                        todo_id, sub_task_id, type, title,
                        pr_url, pr_number, branch_name, status, target_repo_name
                    )
                    VALUES ($1, $2, 'pull_request', $3, $4, $5, $6, 'pending', $7)
                    """,
                    coord.todo_id,
                    sub_task["id"],
                    f"PR: {todo['title']}",
                    pr_info.get("url"),
                    pr_info.get("number"),
                    branch_name,
                    dep_repo_name,
                )
            else:
                logger.warning("[%s] create_pr returned empty result", coord.todo_id)
                await coord._post_system_message("**PR creation failed:** git provider returned no PR data.")

            return pr_info
        except Exception as exc:
            logger.error("[%s] Failed to finalize subtask workspace", coord.todo_id, exc_info=True)
            err_detail = str(exc)[:500]
            await coord._post_system_message(f"**PR creation failed:** {err_detail}")
            return None

    # ------------------------------------------------------------------
    # Template interpolation (used by deployer)
    # ------------------------------------------------------------------

    def interpolate_template(self, template: str, variables: dict[str, str]) -> str:
        """Replace {{key}} placeholders in template strings with variable values."""
        def replacer(match):
            key = match.group(1).strip()
            return variables.get(key, match.group(0))
        return re.sub(r'\{\{(\w+)\}\}', replacer, template)

    # ------------------------------------------------------------------
    # Release subtask creation
    # ------------------------------------------------------------------

    async def create_release_subtasks(self, merge_subtask: dict, project_settings: dict) -> None:
        """Create chained release pipeline subtasks after a successful merge."""
        coord = self._coord
        release_config = project_settings.get("release_config", {})
        if not release_config:
            logger.info("[%s] No release_config found, skipping release subtasks", coord.todo_id)
            return

        merge_st_id = str(merge_subtask["id"])
        target_repo_json = merge_subtask.get("target_repo")
        if isinstance(target_repo_json, str):
            target_repo_json = json.loads(target_repo_json)

        max_order = await coord.db.fetchval(
            "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
            coord.todo_id,
        )

        # 1. Build watcher subtask (depends on merge)
        build_row = await coord.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'release_build_watcher', $4, $5, $6)
            RETURNING id
            """,
            coord.todo_id,
            "Watch build pipeline",
            "Monitor CI/CD build pipeline after merge and capture build artifact hash.",
            max_order + 1,
            [merge_st_id],
            target_repo_json,
        )
        build_watcher_id = str(build_row["id"])
        logger.info("[%s] Created release_build_watcher sub-task %s (depends on merge %s)",
                     coord.todo_id, build_watcher_id, merge_st_id)

        last_dep_id = build_watcher_id
        current_order = max_order + 2

        # 2. Test/staging deployer (if enabled)
        test_config = release_config.get("test_release", {})
        if test_config.get("enabled"):
            test_row = await coord.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, target_repo
                )
                VALUES ($1, $2, $3, 'release_deployer', $4, $5, $6)
                RETURNING id
                """,
                coord.todo_id,
                "Deploy to test/staging",
                "Deploy build artifact to test/staging environment. Environment: test",
                current_order,
                [last_dep_id],
                target_repo_json,
            )
            last_dep_id = str(test_row["id"])
            current_order += 1
            logger.info("[%s] Created release_deployer (test) sub-task %s", coord.todo_id, last_dep_id)

        # 3. Prod deployer (if enabled)
        prod_config = release_config.get("prod_release", {})
        if prod_config.get("enabled"):
            await coord.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, target_repo
                )
                VALUES ($1, $2, $3, 'release_deployer', $4, $5, $6)
                RETURNING id
                """,
                coord.todo_id,
                "Deploy to production",
                "Deploy build artifact to production environment. Environment: prod",
                current_order,
                [last_dep_id],
                target_repo_json,
            )
            logger.info("[%s] Created release_deployer (prod) sub-task %s", coord.todo_id, last_dep_id)

        await coord._post_system_message(
            "**Release pipeline:** Created release sub-tasks (build watcher"
            + (", test deploy" if test_config.get("enabled") else "")
            + (", prod deploy" if prod_config.get("enabled") else "")
            + ")."
        )

    # ------------------------------------------------------------------
    # Role-specific execution handlers (registered with SubtaskDispatcher)
    # ------------------------------------------------------------------

    async def execute_pr_creator_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural PR creator: commit, push, create PR, then spawn merge_agent."""
        coord = self._coord
        st_id = str(sub_task["id"])
        await coord._transition_subtask(st_id, "assigned")
        await coord._transition_subtask(st_id, "running")

        await coord.db.execute(
            "UPDATE todo_items SET sub_state = 'creating_pr', updated_at = NOW() WHERE id = $1",
            coord.todo_id,
        )

        try:
            if not workspace_path:
                raise ValueError("No workspace path available for PR creation")

            await coord._report_progress(st_id, 10, "Preparing to commit and push")

            coder_st = await coord.db.fetchrow(
                "SELECT * FROM sub_tasks WHERE todo_id = $1 AND agent_role = 'coder' "
                "AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
                coord.todo_id,
            )
            commit_st = coder_st or sub_task

            await coord._report_progress(st_id, 30, "Committing and pushing changes")
            pr_info = await self.finalize_subtask_workspace(commit_st, workspace_path)

            if not pr_info:
                raise ValueError("PR creation failed — no PR data returned")

            # Handle pre-commit hook failure — create a fix subtask and retry
            if pr_info.get("_pre_commit_failed"):
                pre_commit_output = pr_info.get("pre_commit_output", "")
                logger.info("[%s] Pre-commit hooks failed, creating fix subtask", coord.todo_id)

                fix_description = self._build_pre_commit_fix_description(pre_commit_output)
                fix_id = await self.create_guardrail_subtask(
                    title="Fix: pre-commit hook errors",
                    description=fix_description,
                    role="coder",
                    depends_on=[],
                )

                await coord._post_system_message(
                    f"**Pre-commit hooks failed.** Created fix subtask to resolve the errors.\n\n"
                    f"```\n{pre_commit_output[:2000]}\n```"
                )

                # Reset PR creator to pending, blocked on the fix subtask
                await coord.db.execute(
                    "UPDATE sub_tasks SET status = 'pending', depends_on = $2, updated_at = NOW() WHERE id = $1",
                    sub_task["id"],
                    [fix_id],
                )
                logger.info(
                    "[%s] PR creator %s reset to pending, blocked on fix subtask %s",
                    coord.todo_id, st_id, fix_id,
                )
                return

            pr_url = pr_info.get("url", "N/A")
            await coord._report_progress(st_id, 80, f"PR created: {pr_url}")

            # Store head_sha for release pipeline
            pr_number = pr_info.get("number")
            if pr_number:
                try:
                    todo_pr = await coord._load_todo()
                    project_pr = await coord.db.fetchrow(
                        "SELECT * FROM projects WHERE id = $1", todo_pr["project_id"]
                    )
                    # Use subtask's target_repo to resolve the correct git provider
                    pr_git, pr_owner, pr_repo = await self._resolve_git_for_subtask(
                        sub_task, project_pr,
                    )
                    pr_data = await pr_git.get_pull_request(pr_owner, pr_repo, pr_number)
                    if pr_data and pr_data.get("head_sha"):
                        await coord.db.execute(
                            "UPDATE deliverables SET head_sha = $2 "
                            "WHERE todo_id = $1 AND type = 'pull_request' AND pr_number = $3",
                            coord.todo_id, pr_data["head_sha"], pr_number,
                        )
                        logger.info("[%s] Stored head_sha %s for PR #%s", coord.todo_id, pr_data["head_sha"][:12], pr_number)
                except Exception as e:
                    logger.warning("[%s] Failed to store head_sha for PR: %s", coord.todo_id, e)

            if await self._requires_merge_approval():
                # No merge subtask — user merges the PR manually
                await coord._post_system_message(
                    f"**PR created:** {pr_url}\n\n"
                    "Auto-merge is disabled. Review and merge the PR manually when ready."
                )
                await coord._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message=f"PR: {pr_url}",
                )
                return

            # Auto-merge: create merge_agent subtask
            all_coder_ids = [str(sub_task["id"])]
            if coder_st:
                all_coder_ids.append(str(coder_st["id"]))

            target_repo_json = sub_task.get("target_repo")
            if isinstance(target_repo_json, str):
                target_repo_json = json.loads(target_repo_json)

            max_order = await coord.db.fetchval(
                "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
                coord.todo_id,
            )

            await coord._post_system_message(
                f"**PR created:** {pr_url}. Creating merge sub-task."
            )

            merge_row = await coord.db.fetchrow(
                """
                INSERT INTO sub_tasks (
                    todo_id, title, description, agent_role,
                    execution_order, depends_on, target_repo
                )
                VALUES ($1, $2, $3, 'merge_agent', $4, $5, $6)
                RETURNING id
                """,
                coord.todo_id,
                "Merge PR",
                "Merge the PR. Check CI status, merge, and run post-merge builds if configured.",
                max_order + 1,
                all_coder_ids,
                target_repo_json,
            )

            # Propagate: tasks depending on pr_creator should also wait for merge
            await self._propagate_dependencies(st_id, [str(merge_row["id"])])

            await coord._report_progress(st_id, 100, "PR created successfully")
            await coord._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"PR: {pr_url}",
            )

        except Exception as e:
            logger.error("[%s] PR creator failed: %s", coord.todo_id, e, exc_info=True)
            await coord._post_system_message(
                f"**PR creation failed:** {str(e)[:500]}. You can retry this sub-task."
            )
            await coord._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    async def execute_merge_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural merge agent: check CI, merge PR, run post-merge builds."""
        coord = self._coord
        st_id = str(sub_task["id"])
        await coord._transition_subtask(st_id, "assigned")
        await coord._transition_subtask(st_id, "running")

        await coord.db.execute(
            "UPDATE todo_items SET sub_state = CASE "
            "WHEN sub_state = 'merge_approved' THEN 'merge_approved' "
            "ELSE 'merging' END, updated_at = NOW() WHERE id = $1",
            coord.todo_id,
        )

        try:
            todo = await coord._load_todo()
            project = await coord.db.fetchrow(
                "SELECT * FROM projects WHERE id = $1", todo["project_id"]
            )
            if not project or not project.get("repo_url"):
                raise ValueError("No repo configured for merge")

            # Resolve target_repo to filter the correct PR deliverable
            target_repo = sub_task.get("target_repo")
            if isinstance(target_repo, str):
                target_repo = json.loads(target_repo)
            target_repo_name = target_repo.get("name") if target_repo else None

            if target_repo_name:
                pr_deliv = await coord.db.fetchrow(
                    "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                    "AND pr_number IS NOT NULL AND target_repo_name = $2 "
                    "ORDER BY created_at DESC LIMIT 1",
                    coord.todo_id, target_repo_name,
                )
            else:
                pr_deliv = await coord.db.fetchrow(
                    "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                    "AND pr_number IS NOT NULL AND target_repo_name IS NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    coord.todo_id,
                )
            if not pr_deliv:
                await coord._post_system_message("**Merge agent:** No PR found to merge. Skipping.")
                await coord._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message="No PR to merge",
                )
                return

            # Resolve git provider from subtask's target_repo (dep) or project (main)
            git, owner, repo = await self._resolve_git_for_subtask(sub_task, project)
            pr_number = pr_deliv["pr_number"]

            await coord._report_progress(st_id, 20, "Checking PR status")

            # 1. Get PR status
            pr_data = await git.get_pull_request(owner, repo, pr_number)
            if pr_data["state"] != "open":
                await coord._post_system_message(
                    f"**Merge agent:** PR #{pr_number} is {pr_data['state']}, not open. Skipping merge."
                )
                await coord._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message=f"PR already {pr_data['state']}",
                )
                return

            # 2. Check CI status
            await coord._report_progress(st_id, 40, "Checking CI status")
            ci_data = await git.get_check_runs(owner, repo, pr_data["head_sha"])

            if ci_data["state"] == "pending":
                await coord._post_system_message(
                    f"**Merge agent:** CI still running for PR #{pr_number}. Will retry on next cycle."
                )
                await coord._transition_subtask(
                    st_id, "pending",
                    progress_message="Waiting for CI",
                )
                return

            if ci_data["state"] == "failure":
                failed_checks = [c["name"] for c in ci_data.get("checks", []) if c.get("conclusion") == "failure"]
                msg = f"CI failed: {', '.join(failed_checks)}" if failed_checks else "CI checks failed"
                await git.post_pr_comment(
                    owner, repo, pr_number,
                    f"**Agent Merge Bot:** Cannot merge — {msg}",
                )
                await coord._post_system_message(
                    f"**Merge agent:** CI failed for PR #{pr_number}. {msg}"
                )
                await coord._transition_subtask(
                    st_id, "failed",
                    error_message=msg,
                )
                return

            # 3. Check for unmerged dependency PRs
            await coord._report_progress(st_id, 50, "Checking dependency PRs")
            dep_subtasks = await coord.db.fetch(
                "SELECT d.pr_state, d.target_repo_name, d.pr_number "
                "FROM sub_tasks st JOIN deliverables d ON d.sub_task_id = st.id "
                "WHERE st.todo_id = $1 AND st.target_repo IS NOT NULL "
                "AND d.type = 'pull_request' AND d.pr_state != 'merged'",
                coord.todo_id,
            )
            if dep_subtasks:
                dep_names = [d.get("target_repo_name") or f"PR #{d['pr_number']}" for d in dep_subtasks]
                await coord._post_system_message(
                    f"**Merge agent:** Waiting for dependency PRs: {', '.join(dep_names)}"
                )
                await coord._transition_subtask(
                    st_id, "pending",
                    progress_message="Waiting for dependency PRs",
                )
                return

            # 4. Check if human approval is required
            project_settings = project.get("settings_json") or {}
            if isinstance(project_settings, str):
                project_settings = json.loads(project_settings)

            require_approval = project_settings.get("require_merge_approval", False)
            already_approved = todo.get("sub_state") == "merge_approved"

            logger.info(
                "Merge approval check: todo=%s pr=#%d require=%s approved=%s sub_state=%s",
                coord.todo_id[:8], pr_number, require_approval, already_approved,
                todo.get("sub_state"),
            )

            if require_approval and not already_approved:
                await coord.db.execute(
                    "UPDATE todo_items SET sub_state = 'awaiting_merge_approval', updated_at = NOW() WHERE id = $1",
                    coord.todo_id,
                )
                await coord._post_system_message(
                    f"**PR #{pr_number} is ready to merge.** CI passed. Awaiting your approval to merge."
                )
                await coord._transition_subtask(
                    st_id, "pending",
                    progress_message="Awaiting merge approval",
                )
                await coord.redis.publish(
                    f"task:{coord.todo_id}:events",
                    json.dumps({
                        "type": "state_change",
                        "state": "in_progress",
                        "sub_state": "awaiting_merge_approval",
                    }),
                )
                return

            if already_approved:
                await coord.db.execute(
                    "UPDATE todo_items SET sub_state = 'merging', updated_at = NOW() WHERE id = $1",
                    coord.todo_id,
                )

            # 5. Merge the PR
            await coord._report_progress(st_id, 70, f"Merging PR #{pr_number}")
            merge_method = project_settings.get("merge_method", "squash")

            await self.verify_merge_authorization(pr_number)

            merge_result = await git.merge_pull_request(
                owner, repo, pr_number, method=merge_method,
            )

            if not merge_result.get("merged"):
                await coord._post_system_message(
                    f"**Merge agent:** Failed to merge PR #{pr_number}: {merge_result.get('message', 'unknown error')}"
                )
                await coord._transition_subtask(
                    st_id, "failed",
                    error_message=merge_result.get("message", "Merge failed"),
                )
                return

            # 6. Update deliverable
            await coord.db.execute(
                "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
                "merge_method = $2, status = 'approved' WHERE id = $1",
                pr_deliv["id"],
                merge_method,
            )

            await coord._post_system_message(
                f"**PR #{pr_number} merged** via {merge_method}. SHA: {merge_result.get('sha', 'N/A')}"
            )

            # 7. Post-merge build commands
            build_commands = project_settings.get("build_commands", [])
            if build_commands and workspace_path:
                await coord._report_progress(st_id, 85, "Running post-merge builds")
                await self.run_post_merge_builds(todo, build_commands, workspace_path)

            await coord._report_progress(st_id, 100, "Merge complete")
            await coord._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message="Merged",
            )

            # Trigger release pipeline if enabled
            if project_settings.get("release_pipeline_enabled"):
                try:
                    await self.create_release_subtasks(sub_task, project_settings)
                except Exception as e:
                    logger.error("Failed to create release subtasks: %s", e, exc_info=True)
                    await coord._post_system_message(
                        f"**Release pipeline:** Failed to create release sub-tasks: {str(e)[:300]}"
                    )

        except Exception as e:
            logger.error("Merge agent failed: %s", e, exc_info=True)
            await coord._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    async def verify_merge_authorization(self, pr_number: int) -> None:
        """Final guard: re-verify approval from DB before irreversible merge."""
        coord = self._coord
        row = await coord.db.fetchrow(
            "SELECT t.sub_state, p.settings_json "
            "FROM todo_items t JOIN projects p ON t.project_id = p.id "
            "WHERE t.id = $1",
            coord.todo_id,
        )
        proj_settings = row["settings_json"] or {}
        if isinstance(proj_settings, str):
            proj_settings = json.loads(proj_settings)

        require = proj_settings.get("require_merge_approval", False)
        sub_state = row["sub_state"]

        if require and sub_state not in ("merge_approved", "merging"):
            logger.error(
                "MERGE BLOCKED: todo=%s pr=#%d require_merge_approval=True sub_state=%s",
                coord.todo_id[:8], pr_number, sub_state,
            )
            raise RuntimeError(
                f"Merge not authorized: approval required but sub_state is '{sub_state}'"
            )

        logger.info(
            "Merge authorization passed: todo=%s pr=#%d require=%s sub_state=%s",
            coord.todo_id[:8], pr_number, require, sub_state,
        )

    async def run_post_merge_builds(
        self, todo: dict, build_commands: list[str], workspace_path: str,
    ) -> None:
        """Pull latest after merge and run build commands."""
        coord = self._coord
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "origin",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.communicate()

        for cmd in build_commands:
            try:
                exit_code, output = await coord.workspace_mgr.run_command(cmd, repo_dir, timeout=120)
                if exit_code != 0:
                    await coord._post_system_message(
                        f"**Post-merge build failed:** `{cmd}`\n```\n{output[:500]}\n```"
                    )
                else:
                    logger.info("Post-merge build passed: %s", cmd)
            except Exception as e:
                await coord._post_system_message(
                    f"**Post-merge build error:** `{cmd}` — {str(e)[:200]}"
                )

    # ------------------------------------------------------------------
    # Merge observer (watches for external merge, never merges itself)
    # ------------------------------------------------------------------

    async def _check_pr_merged(self, git, owner: str, repo: str, pr_number: int, pr_data: dict) -> bool:
        """Check if a closed PR was actually merged (not just closed)."""
        # GitLab: state == "merged" directly
        if pr_data.get("state") == "merged":
            return True
        # GitHub: check merge endpoint (204 = merged, 404 = not merged)
        if hasattr(git, "http"):
            try:
                resp = await git.http.get(
                    f"{git.api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                )
                return resp.status_code == 204
            except Exception:
                pass
        # Fallback: check merged flag
        return bool(pr_data.get("merged"))

    async def execute_merge_observer_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Watch for a PR to be merged externally. Never merges itself."""
        coord = self._coord
        st_id = str(sub_task["id"])
        await coord._transition_subtask(st_id, "assigned")
        await coord._transition_subtask(st_id, "running")

        await coord.db.execute(
            "UPDATE todo_items SET sub_state = 'awaiting_external_merge', "
            "updated_at = NOW() WHERE id = $1",
            coord.todo_id,
        )

        try:
            todo = await coord._load_todo()
            project = await coord.db.fetchrow(
                "SELECT * FROM projects WHERE id = $1", todo["project_id"]
            )
            if not project or not project.get("repo_url"):
                raise ValueError("No repo configured for merge observation")

            # Resolve target_repo for correct PR lookup
            target_repo = sub_task.get("target_repo")
            if isinstance(target_repo, str):
                target_repo = json.loads(target_repo)
            target_repo_name = target_repo.get("name") if target_repo else None

            if target_repo_name:
                pr_deliv = await coord.db.fetchrow(
                    "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                    "AND pr_number IS NOT NULL AND target_repo_name = $2 "
                    "ORDER BY created_at DESC LIMIT 1",
                    coord.todo_id, target_repo_name,
                )
            else:
                pr_deliv = await coord.db.fetchrow(
                    "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                    "AND pr_number IS NOT NULL AND target_repo_name IS NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    coord.todo_id,
                )

            if not pr_deliv:
                raise ValueError("No PR deliverable found to observe")

            git, owner, repo = await self._resolve_git_for_subtask(sub_task, project)
            pr_number = pr_deliv["pr_number"]

            await coord._post_system_message(
                f"**Merge observer:** Watching PR #{pr_number} for external merge. "
                "Auto-merge is disabled — a human must merge this PR."
            )

            await coord.redis.publish(
                f"task:{coord.todo_id}:events",
                json.dumps({
                    "type": "state_change",
                    "state": "in_progress",
                    "sub_state": "awaiting_external_merge",
                }),
            )

            poll_interval = 3600   # 1 hour
            timeout_hours = 72     # 3 days
            deadline = time.monotonic() + timeout_hours * 3600
            wake_key = f"merge_observer:{coord.todo_id}:wake"

            while time.monotonic() < deadline:
                # Clear any pending wake signal
                await coord.redis.delete(wake_key)

                # Poll PR status
                pr_data = await git.get_pull_request(owner, repo, pr_number)

                if pr_data.get("state") in ("closed", "merged"):
                    merged = await self._check_pr_merged(git, owner, repo, pr_number, pr_data)
                    if merged:
                        await coord.db.execute(
                            "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
                            "status = 'approved' WHERE id = $1",
                            pr_deliv["id"],
                        )
                        merge_sha = pr_data.get("merge_commit_sha", pr_data.get("head_sha", ""))
                        await coord._post_system_message(
                            f"**PR #{pr_number} merged externally.** SHA: {merge_sha[:12]}"
                        )
                        await coord._transition_subtask(
                            st_id, "completed",
                            progress_pct=100, progress_message="Merged externally",
                        )
                        await coord.db.execute(
                            "UPDATE todo_items SET sub_state = NULL, updated_at = NOW() WHERE id = $1",
                            coord.todo_id,
                        )

                        # Trigger release pipeline if enabled
                        project_settings = project.get("settings_json") or {}
                        if isinstance(project_settings, str):
                            project_settings = json.loads(project_settings)
                        if project_settings.get("release_pipeline_enabled"):
                            try:
                                await self.create_release_subtasks(sub_task, project_settings)
                            except Exception as rel_e:
                                logger.error("[%s] Failed to create release subtasks: %s", coord.todo_id, rel_e)
                        return
                    else:
                        # PR closed without merge
                        await coord._post_system_message(
                            f"**PR #{pr_number} was closed without merging.**"
                        )
                        await coord._transition_subtask(
                            st_id, "failed",
                            error_message="PR closed without merge",
                        )
                        return

                # Still open — update progress and keep alive
                await coord._report_progress(
                    st_id, 20,
                    f"PR #{pr_number} still open. Next check in {poll_interval // 60}m.",
                )
                await coord.db.execute(
                    "UPDATE todo_items SET updated_at = NOW() WHERE id = $1",
                    coord.todo_id,
                )

                # Sleep in short increments, checking for webhook wake signal
                sleep_end = time.monotonic() + poll_interval
                while time.monotonic() < sleep_end and time.monotonic() < deadline:
                    woken = await coord.redis.get(wake_key)
                    if woken:
                        await coord.redis.delete(wake_key)
                        logger.info("[%s] Merge observer woken by webhook", coord.todo_id)
                        break
                    await asyncio.sleep(30)

            # Timed out
            await coord._post_system_message(
                f"**Merge observer:** Timed out after {timeout_hours}h waiting for PR #{pr_number}."
            )
            await coord._transition_subtask(
                st_id, "failed",
                error_message=f"Timed out after {timeout_hours}h waiting for external merge",
            )

        except Exception as e:
            logger.error("[%s] Merge observer failed: %s", coord.todo_id, e, exc_info=True)
            await coord._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    # ------------------------------------------------------------------
    # Build watcher
    # ------------------------------------------------------------------

    async def execute_build_watcher_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural build watcher: poll CI/CD for build completion."""
        import httpx as _httpx

        coord = self._coord
        st_id = str(sub_task["id"])
        await coord._transition_subtask(st_id, "assigned")
        await coord._transition_subtask(st_id, "running")

        await coord.db.execute(
            "UPDATE todo_items SET sub_state = 'build_watching', updated_at = NOW() WHERE id = $1",
            coord.todo_id,
        )

        try:
            todo = await coord._load_todo()
            project = await coord.db.fetchrow(
                "SELECT * FROM projects WHERE id = $1", todo["project_id"]
            )
            if not project or not project.get("repo_url"):
                raise ValueError("No repo configured for build watching")

            project_settings = project.get("settings_json") or {}
            if isinstance(project_settings, str):
                project_settings = json.loads(project_settings)
            release_config = project_settings.get("release_config", {})
            build_config = release_config.get("build", {})

            pr_deliv = await coord.db.fetchrow(
                "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "AND pr_state = 'merged' ORDER BY created_at DESC LIMIT 1",
                coord.todo_id,
            )
            if not pr_deliv:
                raise ValueError("No merged PR deliverable found for build watching")

            await coord.notifier.notify(
                str(todo["creator_id"]),
                "build_started",
                {
                    "todo_id": coord.todo_id,
                    "title": todo["title"],
                    "detail": "Build pipeline started after merge.",
                },
            )

            await coord._report_progress(st_id, 10, "Monitoring build pipeline")

            git, owner, repo = await coord._resolve_git_provider(project)

            build_provider = build_config.get("provider", "github_actions")
            poll_interval = build_config.get("poll_interval_seconds", 30)
            timeout_minutes = build_config.get("timeout_minutes", 30)
            deadline = time.monotonic() + timeout_minutes * 60
            image_hash = None

            if build_provider == "github_actions":
                workflow_name = build_config.get("workflow_name")
                head_sha = pr_deliv.get("head_sha") or ""

                while time.monotonic() < deadline:
                    resp = await git.http.get(
                        f"{git.api_base_url}/repos/{owner}/{repo}/actions/runs",
                        params={"event": "push", "per_page": "5"},
                    )
                    resp.raise_for_status()
                    runs = resp.json().get("workflow_runs", [])

                    if workflow_name:
                        runs = [r for r in runs if r.get("name") == workflow_name]

                    target_run = None
                    for run in runs:
                        if head_sha and run.get("head_sha") == head_sha:
                            target_run = run
                            break
                    if not target_run and runs:
                        target_run = runs[0]

                    if target_run:
                        status = target_run.get("status")
                        conclusion = target_run.get("conclusion")

                        if status == "completed":
                            if conclusion == "success":
                                run_id = target_run["id"]
                                try:
                                    art_resp = await git.http.get(
                                        f"{git.api_base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
                                    )
                                    art_resp.raise_for_status()
                                    artifacts = art_resp.json().get("artifacts", [])
                                    for artifact in artifacts:
                                        name = (artifact.get("name") or "").lower()
                                        if "digest" in name or "hash" in name:
                                            image_hash = artifact.get("name")
                                            break
                                except Exception:
                                    logger.debug("Could not fetch artifacts for run %s", run_id)

                                if not image_hash:
                                    image_hash = target_run.get("head_sha", head_sha)

                                logger.info("[%s] Build succeeded, image_hash=%s", coord.todo_id, image_hash)
                                break
                            else:
                                raise ValueError(
                                    f"Build failed with conclusion '{conclusion}': {target_run.get('html_url', 'N/A')}"
                                )

                    await coord._report_progress(
                        st_id, 30, f"Build in progress... (polling every {poll_interval}s)"
                    )
                    await asyncio.sleep(poll_interval)
                else:
                    raise ValueError(f"Build timed out after {timeout_minutes} minutes")

            elif build_provider == "jenkins":
                job_url = build_config.get("job_url", "").rstrip("/")
                jenkins_token = build_config.get("token")
                if not job_url:
                    raise ValueError("Jenkins job_url not configured in build config")

                headers = {}
                if jenkins_token:
                    headers["Authorization"] = f"Bearer {jenkins_token}"

                async with _httpx.AsyncClient(timeout=30, headers=headers) as client:
                    resp = await client.get(f"{job_url}/api/json")
                    resp.raise_for_status()
                    initial_last = resp.json().get("lastCompletedBuild", {}).get("number", 0)

                    while time.monotonic() < deadline:
                        resp = await client.get(f"{job_url}/api/json")
                        resp.raise_for_status()
                        data = resp.json()
                        last_completed = data.get("lastCompletedBuild", {}).get("number", 0)

                        if last_completed > initial_last:
                            build_resp = await client.get(
                                f"{job_url}/{last_completed}/api/json"
                            )
                            build_resp.raise_for_status()
                            build_data = build_resp.json()
                            result = build_data.get("result", "")

                            if result == "SUCCESS":
                                desc = build_data.get("description") or ""
                                if "sha256:" in desc:
                                    image_hash = desc[desc.index("sha256:"):].split()[0]
                                else:
                                    image_hash = str(last_completed)
                                logger.info("[%s] Jenkins build %d succeeded, hash=%s",
                                            coord.todo_id, last_completed, image_hash)
                                break
                            else:
                                raise ValueError(
                                    f"Jenkins build #{last_completed} failed with result: {result}"
                                )

                        await coord._report_progress(
                            st_id, 30, f"Waiting for Jenkins build... (polling every {poll_interval}s)"
                        )
                        await asyncio.sleep(poll_interval)
                    else:
                        raise ValueError(f"Jenkins build timed out after {timeout_minutes} minutes")

            else:
                raise ValueError(f"Unsupported build provider: {build_provider}")

            # Store artifact in deliverables
            artifact_data = {"image_hash": image_hash, "build_provider": build_provider}
            await coord.db.execute(
                "UPDATE deliverables SET release_artifact_json = $2, head_sha = COALESCE(head_sha, $3) "
                "WHERE id = $1",
                pr_deliv["id"],
                artifact_data,
                image_hash,
            )

            await coord.notifier.notify(
                str(todo["creator_id"]),
                "build_completed",
                {
                    "todo_id": coord.todo_id,
                    "title": todo["title"],
                    "detail": f"Build completed successfully. Artifact: {image_hash}",
                },
            )

            await coord._report_progress(st_id, 100, "Build completed")
            await coord._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"Build complete: {image_hash}",
                output_result={"image_hash": image_hash},
            )

        except Exception as e:
            logger.error("[%s] Build watcher failed: %s", coord.todo_id, e, exc_info=True)
            try:
                todo = await coord._load_todo()
                await coord.notifier.notify(
                    str(todo["creator_id"]),
                    "build_failed",
                    {
                        "todo_id": coord.todo_id,
                        "title": todo["title"],
                        "detail": f"Build failed: {str(e)[:300]}",
                    },
                )
            except Exception:
                pass
            await coord._post_system_message(
                f"**Release pipeline:** Build failed: {str(e)[:500]}"
            )
            await coord._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )

    # ------------------------------------------------------------------
    # Release deployer
    # ------------------------------------------------------------------

    async def execute_release_deployer_subtask(
        self, sub_task: dict, provider: AIProvider, workspace_path: str | None,
    ) -> None:
        """Procedural release deployer: trigger deployment via configured HTTP endpoint."""
        import httpx as _httpx

        coord = self._coord
        st_id = str(sub_task["id"])
        await coord._transition_subtask(st_id, "assigned")
        await coord._transition_subtask(st_id, "running")

        description = (sub_task.get("description") or "").lower()
        is_prod = "prod" in description
        env_name = "prod" if is_prod else "test"

        await coord.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            coord.todo_id,
            f"releasing_{env_name}",
        )

        try:
            todo = await coord._load_todo()
            project = await coord.db.fetchrow(
                "SELECT * FROM projects WHERE id = $1", todo["project_id"]
            )
            if not project:
                raise ValueError("Project not found")

            project_settings = project.get("settings_json") or {}
            if isinstance(project_settings, str):
                project_settings = json.loads(project_settings)
            release_config = project_settings.get("release_config", {})

            config_key = "prod_release" if is_prod else "test_release"
            env_config = release_config.get(config_key, {})
            if not env_config.get("enabled"):
                logger.info("[%s] Release %s not enabled, skipping", coord.todo_id, env_name)
                await coord._transition_subtask(
                    st_id, "completed",
                    progress_pct=100, progress_message=f"{env_name} release skipped (not enabled)",
                )
                return

            # Approval gate for production
            if is_prod and env_config.get("require_approval"):
                current_sub_state = todo.get("sub_state")
                if current_sub_state != "release_prod_approved":
                    await coord.db.execute(
                        "UPDATE todo_items SET sub_state = 'awaiting_release_approval', updated_at = NOW() WHERE id = $1",
                        coord.todo_id,
                    )
                    await coord._post_system_message(
                        "**Release pipeline:** Production deployment is ready. Awaiting your approval to deploy to production."
                    )
                    await coord._transition_subtask(
                        st_id, "pending",
                        progress_message="Awaiting production release approval",
                    )
                    await coord.redis.publish(
                        f"task:{coord.todo_id}:events",
                        json.dumps({
                            "type": "state_change",
                            "state": "in_progress",
                            "sub_state": "awaiting_release_approval",
                        }),
                    )
                    return
                else:
                    await coord.db.execute(
                        "UPDATE todo_items SET sub_state = 'releasing_prod', updated_at = NOW() WHERE id = $1",
                        coord.todo_id,
                    )

            await coord._report_progress(st_id, 20, f"Preparing {env_name} deployment")

            # Get image_hash from build_watcher's output_result
            build_st = await coord.db.fetchrow(
                "SELECT output_result FROM sub_tasks WHERE todo_id = $1 "
                "AND agent_role = 'release_build_watcher' AND status = 'completed' "
                "ORDER BY created_at DESC LIMIT 1",
                coord.todo_id,
            )
            if not build_st:
                raise ValueError("No completed build_watcher subtask found")

            build_output = build_st.get("output_result") or {}
            if isinstance(build_output, str):
                build_output = json.loads(build_output)
            image_hash = build_output.get("image_hash", "")

            pr_deliv = await coord.db.fetchrow(
                "SELECT head_sha FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "ORDER BY created_at DESC LIMIT 1",
                coord.todo_id,
            )
            commit_sha = (pr_deliv["head_sha"] if pr_deliv and pr_deliv.get("head_sha") else "")

            project_name = project.get("name", "")
            variables = {
                "image_hash": image_hash,
                "commit_sha": commit_sha,
                "env": env_name,
                "project_name": project_name,
                "todo_id": coord.todo_id,
            }

            api_url = self.interpolate_template(env_config.get("api_url", ""), variables)
            method = env_config.get("http_method", "POST").upper()

            raw_headers = env_config.get("headers", {})
            headers = {}
            for k, v in raw_headers.items():
                headers[k] = self.interpolate_template(str(v), variables)

            body_template = env_config.get("body_template", "")
            body_str = self.interpolate_template(body_template, variables)

            success_codes = env_config.get("success_status_codes", [200, 201, 202])

            await coord._report_progress(st_id, 50, f"Triggering {env_name} deployment")

            async with _httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(
                    method=method,
                    url=api_url,
                    headers=headers,
                    content=body_str,
                )

                if resp.status_code not in success_codes:
                    raise ValueError(
                        f"Deployment request failed with status {resp.status_code}: {resp.text[:500]}"
                    )

                deploy_response = {}
                try:
                    deploy_response = resp.json()
                except Exception:
                    deploy_response = {"raw": resp.text[:1000]}

                logger.info("[%s] %s deployment triggered successfully: %s",
                            coord.todo_id, env_name, resp.status_code)

                # Poll status URL if configured
                poll_url = env_config.get("poll_status_url")
                if poll_url:
                    release_id = str(deploy_response.get("release_id", deploy_response.get("id", "")))
                    poll_variables = {**variables, "release_id": release_id}
                    resolved_poll_url = self.interpolate_template(poll_url, poll_variables)
                    poll_success = env_config.get("poll_success_value", "succeeded")
                    poll_interval_secs = env_config.get("poll_interval_seconds", 15)
                    poll_timeout = env_config.get("poll_timeout_minutes", 15)
                    poll_deadline = time.monotonic() + poll_timeout * 60

                    await coord._report_progress(st_id, 70, f"Waiting for {env_name} deployment to complete")

                    while time.monotonic() < poll_deadline:
                        poll_resp = await client.get(resolved_poll_url, headers=headers)
                        if poll_resp.status_code == 200:
                            try:
                                poll_data = poll_resp.json()
                                status_val = str(poll_data.get("status", "")).lower()
                                if status_val == poll_success.lower():
                                    logger.info("[%s] %s deployment poll: succeeded", coord.todo_id, env_name)
                                    break
                                if status_val in ("failed", "error", "cancelled"):
                                    raise ValueError(
                                        f"Deployment failed during polling: status={status_val}"
                                    )
                            except ValueError:
                                raise
                            except Exception:
                                pass
                        await asyncio.sleep(poll_interval_secs)
                    else:
                        raise ValueError(f"Deployment status poll timed out after {poll_timeout} minutes")

            await coord.notifier.notify(
                str(todo["creator_id"]),
                f"release_{env_name}_completed",
                {
                    "todo_id": coord.todo_id,
                    "title": todo["title"],
                    "detail": f"Successfully deployed to {env_name}. Image: {image_hash}",
                },
            )

            await coord._report_progress(st_id, 100, f"{env_name} deployment complete")
            await coord._transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"Deployed to {env_name}",
            )

        except Exception as e:
            logger.error("[%s] Release deployer (%s) failed: %s", coord.todo_id, env_name, e, exc_info=True)
            try:
                todo = await coord._load_todo()
                await coord.notifier.notify(
                    str(todo["creator_id"]),
                    f"release_{env_name}_failed",
                    {
                        "todo_id": coord.todo_id,
                        "title": todo["title"],
                        "detail": f"Deployment to {env_name} failed: {str(e)[:300]}",
                    },
                )
            except Exception:
                pass
            await coord._post_system_message(
                f"**Release pipeline:** {env_name} deployment failed: {str(e)[:500]}"
            )
            await coord._transition_subtask(
                st_id, "failed",
                error_message=str(e)[:500],
            )
