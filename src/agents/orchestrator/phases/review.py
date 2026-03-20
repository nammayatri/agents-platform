"""Review phase — finalize task, create PRs, store memories, complete."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from agents.config.settings import settings

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)


class ReviewPhase:
    """Finalize task: build deterministic summary, create PR if needed, complete."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    async def run(self, todo: dict, provider: AIProvider) -> None:
        """Finalize task: build deterministic summary, create PR if needed, complete."""
        coord = self._coord

        # Re-read current state to avoid stale transitions (e.g. concurrent dispatch)
        current = await coord.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", coord.todo_id,
        )
        if current == "review":
            logger.info("[%s] Already in review state, skipping transition", coord.todo_id)
        elif current in ("in_progress", "testing"):
            await coord._transition_todo("review")
        else:
            logger.warning("[%s] Cannot enter review from state=%s, skipping review phase", coord.todo_id, current)
            return

        # Gather all results and build deterministic summary
        results = await coord._ctx.get_completed_results()
        summary_parts = []
        for r in results:
            role = r.get("agent_role", "?")
            title = r.get("title", "?")
            out = r.get("output_result", {})
            approach = out.get("approach", "") or out.get("summary", "") if isinstance(out, dict) else ""
            summary_parts.append(f"- [{role}] {title}: {approach[:200]}" if approach else f"- [{role}] {title}")
        summary = f"Completed {len(results)} sub-task(s):\n" + "\n".join(summary_parts)

        # Collect all PRs: main repo + dependency repos
        all_pr_urls = []

        # Check for existing PRs (from review-chain flow)
        existing_prs = await coord.db.fetch(
            "SELECT pr_url, branch_name FROM deliverables WHERE todo_id = $1 AND type = 'pull_request'",
            coord.todo_id,
        )
        for ep in existing_prs:
            if ep.get("pr_url"):
                all_pr_urls.append(ep["pr_url"])

        # Create main repo PR only if one doesn't already exist for the main branch
        short_id = str(coord.todo_id)[:8]
        main_branch = f"task/{short_id}"
        has_main_pr = any(ep.get("branch_name") == main_branch for ep in existing_prs)
        if not has_main_pr:
            pr_info = await self._finalize_workspace(todo, summary)
            if pr_info and pr_info.get("url"):
                all_pr_urls.append(pr_info["url"])

        # Dep repo PRs: find subtasks with target_repo and finalize each unique dep workspace
        dep_subtasks = await coord.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND target_repo IS NOT NULL "
            "AND status = 'completed'",
            coord.todo_id,
        )
        finalized_deps = set()  # track by dep name to avoid duplicate PRs
        for dst in dep_subtasks:
            target_repo = dst.get("target_repo")
            if isinstance(target_repo, str):
                target_repo = json.loads(target_repo)
            if not target_repo or not target_repo.get("repo_url"):
                continue
            dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
            if dep_name in finalized_deps:
                continue
            finalized_deps.add(dep_name)
            # Check if PR already exists for this dep branch
            dep_short_id = str(coord.todo_id)[:8]
            dep_branch = f"task/{dep_short_id}-{dep_name}"
            existing_dep_pr = await coord.db.fetchrow(
                "SELECT pr_url FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
                "AND branch_name = $2",
                coord.todo_id, dep_branch,
            )
            if existing_dep_pr:
                if existing_dep_pr.get("pr_url"):
                    all_pr_urls.append(existing_dep_pr["pr_url"])
                continue
            # Finalize the dep workspace
            try:
                project_dir = os.path.join(settings.workspace_root, str(todo["project_id"]))
                dep_task_dir = os.path.join(project_dir, "tasks", str(coord.todo_id), f"dep_{dep_name}")
                if os.path.isdir(dep_task_dir):
                    dep_pr = await coord._lifecycle.finalize_subtask_workspace(dict(dst), dep_task_dir)
                    if dep_pr and dep_pr.get("url"):
                        all_pr_urls.append(dep_pr["url"])
            except Exception:
                logger.warning("[%s] Failed to finalize dep workspace for %s", coord.todo_id, dep_name, exc_info=True)

        # Build summary with all PR URLs
        if all_pr_urls:
            summary += "\n\nPRs created:\n" + "\n".join(f"  - {url}" for url in all_pr_urls)

        pr_note = ""
        if all_pr_urls:
            pr_note = "**PRs:**\n" + "\n".join(f"- {url}" for url in all_pr_urls) + "\n\n"

        await coord._post_system_message(
            f"**Task completed.**\n\n{pr_note}"
            f"{len(results)} sub-task(s) finished successfully."
        )
        await coord._transition_todo(
            "completed",
            result_summary=summary,
        )
        await coord.notifier.notify(
            str(todo["creator_id"]),
            "completed",
            {
                "todo_id": coord.todo_id,
                "title": todo["title"],
                "detail": summary,
            },
        )

        # Extract and store persistent memories from completed task
        try:
            from agents.indexing.memory_extractor import extract_memories, deduplicate_memories
            # Gather iteration logs from all subtasks
            all_iter_logs = []
            for r in results:
                if r.get("iteration_log"):
                    log = r["iteration_log"]
                    if isinstance(log, str):
                        import json as _json
                        log = _json.loads(log)
                    all_iter_logs.extend(log)

            if all_iter_logs:
                memories = await extract_memories(
                    all_iter_logs,
                    task_title=todo["title"],
                    task_summary=summary,
                    provider=provider,
                )
                if memories:
                    # Fetch existing memories for dedup
                    existing = await coord.db.fetch(
                        "SELECT content FROM project_memories WHERE project_id = $1",
                        todo["project_id"],
                    )
                    existing_contents = [row["content"] for row in existing]
                    unique_memories = await deduplicate_memories(memories, existing_contents)

                    for mem in unique_memories:
                        await coord.db.execute(
                            """INSERT INTO project_memories
                               (project_id, category, content, source_todo_id, confidence)
                               VALUES ($1, $2, $3, $4, $5)""",
                            todo["project_id"], mem.category, mem.content,
                            coord.todo_id, mem.confidence,
                        )
                    logger.info(
                        "[%s] Stored %d new project memories (extracted %d, %d deduplicated)",
                        coord.todo_id, len(unique_memories), len(memories),
                        len(memories) - len(unique_memories),
                    )
        except ImportError:
            logger.debug("[%s] memory_extractor not available, skipping memory extraction", coord.todo_id)
        except Exception as e:
            logger.warning("[%s] Failed to extract project memories: %s", coord.todo_id, e)

    # ------------------------------------------------------------------
    # Workspace finalization
    # ------------------------------------------------------------------

    async def _finalize_workspace(self, todo: dict, summary: str) -> dict | None:
        """Commit changes, push branch, and create PR if workspace has changes."""
        coord = self._coord
        try:
            project = await coord.db.fetchrow(
                "SELECT repo_url, default_branch FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if not project or not project.get("repo_url"):
                return None

            # Find the task workspace
            project_dir = os.path.join(settings.workspace_root, str(todo["project_id"]))
            task_dir = os.path.join(project_dir, "tasks", str(coord.todo_id))
            if not os.path.isdir(task_dir):
                return None

            short_id = str(coord.todo_id)[:8]
            branch_name = f"task/{short_id}"
            base_branch = project.get("default_branch") or "main"

            # Commit and push
            commit_result = await coord.workspace_mgr.commit_and_push(
                task_dir,
                message=f"[agents] {todo['title']}\n\n{summary}",
                branch=branch_name,
            )
            if not commit_result["success"]:
                return None

            # Create PR
            pr_info = await coord.workspace_mgr.create_pr(
                str(todo["project_id"]),
                head_branch=branch_name,
                base_branch=base_branch,
                title=todo["title"],
                body=(
                    f"## Summary\n{summary}\n\n"
                    f"---\n*Created by AI Agent Orchestrator*"
                ),
            )

            # Store PR as deliverable
            if pr_info:
                await coord.db.execute(
                    """
                    INSERT INTO deliverables (
                        todo_id, type, title, pr_url, pr_number, branch_name, status
                    )
                    VALUES ($1, 'pull_request', $2, $3, $4, $5, 'pending')
                    """,
                    coord.todo_id,
                    f"PR: {todo['title']}",
                    pr_info.get("url"),
                    pr_info.get("number"),
                    branch_name,
                )

            return pr_info

        except Exception:
            logger.warning("Failed to finalize workspace for %s", coord.todo_id, exc_info=True)
            return None
