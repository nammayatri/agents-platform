"""Context building for specialist agents.

Extracts the prompt-construction logic from the coordinator into a
standalone class so it can be tested and evolved independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import asyncpg

from agents.agents.registry import (
    build_tools_prompt_block,
    get_agent_definition,
    get_default_system_prompt,
)
from agents.config.settings import settings
from agents.orchestrator.workspace import WorkspaceManager
from agents.utils.json_helpers import safe_json

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Builds system / user prompts for specialist agents.

    Owns the methods that were previously ``_build_agent_prompt``,
    ``_build_iteration_context``, ``_build_debug_context_block``, and
    ``_build_tester_context`` on ``AgentCoordinator``.
    """

    def __init__(
        self,
        db: asyncpg.Pool,
        todo_id: str,
        workspace_mgr: WorkspaceManager,
        provider_registry: Any,
    ) -> None:
        self.db = db
        self.todo_id = todo_id
        self.workspace_mgr = workspace_mgr
        self.provider_registry = provider_registry

        # Per-todo-execution caches (lifetime = coordinator lifetime)
        self._cached_memories: list[dict] | None = None
        self._cached_completed_ids: set[str] = set()
        self._cached_completed_results: list[dict] | None = None
        self._cached_file_tree: dict[str, str] = {}  # workspace_path -> tree_text

    # ------------------------------------------------------------------
    # Small helpers (previously on coordinator)
    # ------------------------------------------------------------------

    async def load_todo(self) -> dict:
        row = await self.db.fetchrow(
            "SELECT * FROM todo_items WHERE id = $1", self.todo_id,
        )
        return dict(row)

    async def get_completed_results(self) -> list[dict]:
        rows = await self.db.fetch(
            "SELECT * FROM sub_tasks WHERE todo_id = $1 AND status = 'completed'",
            self.todo_id,
        )
        return [dict(r) for r in rows]

    async def get_completed_results_cached(self) -> list[dict]:
        """Return completed results, re-fetching only if new subtasks completed."""
        current_ids = {
            str(r["id"])
            for r in await self.db.fetch(
                "SELECT id FROM sub_tasks WHERE todo_id = $1 AND status = 'completed'",
                self.todo_id,
            )
        }
        if current_ids != self._cached_completed_ids or self._cached_completed_results is None:
            self._cached_completed_ids = current_ids
            self._cached_completed_results = await self.get_completed_results()
        return self._cached_completed_results

    async def _get_memories_cached(self, project_id: str) -> list[dict]:
        """Fetch project memories once and cache for the coordinator lifetime."""
        if self._cached_memories is None:
            try:
                rows = await self.db.fetch(
                    """
                    SELECT content, category, confidence FROM project_memories
                    WHERE project_id = $1
                    ORDER BY confidence DESC
                    LIMIT 10
                    """,
                    project_id,
                )
                self._cached_memories = [dict(r) for r in rows]
            except Exception:
                logger.debug("[%s] Failed to load project memories", self.todo_id[:8], exc_info=True)
                self._cached_memories = []
        return self._cached_memories

    def _get_file_tree_cached(self, workspace_path: str, max_depth: int = 4) -> str:
        """Cache file tree per workspace path for the coordinator lifetime."""
        if workspace_path not in self._cached_file_tree:
            self._cached_file_tree[workspace_path] = self.workspace_mgr.get_file_tree(
                workspace_path, max_depth=max_depth,
            )
        return self._cached_file_tree[workspace_path]

    @staticmethod
    def filter_rules_for_role(work_rules: dict, role: str) -> dict:
        """Return only the rule categories relevant to the given agent role."""
        defn = get_agent_definition(role)
        categories = defn.tool_rule_categories if defn else ["general"]
        return {cat: work_rules[cat] for cat in categories if cat in work_rules}

    @staticmethod
    def format_rules_for_prompt(rules: dict) -> str:
        """Format work rules into a string block for injection into prompts."""
        if not rules:
            return ""
        parts = ["\n\n## Work Rules (you MUST follow these)\n"]
        for category, items in rules.items():
            if items:
                parts.append(f"### {category.title()}")
                for item in items:
                    parts.append(f"- {item}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Main builder: simple (non-iterative) agent prompt
    # ------------------------------------------------------------------

    async def build_agent_prompt(
        self,
        sub_task: dict,
        todo: dict,
        previous_results: list[dict],
        *,
        workspace_path: str | None = None,
        work_rules: dict | None = None,
        agent_config: dict | None = None,
    ) -> dict:
        """Build system and user prompts for a specialist agent.

        If an agent_config is provided (custom agent), its system_prompt is
        used instead of the hardcoded default for the role.

        Returns ``{"system": str, "user": str}``.
        """
        role = sub_task["agent_role"]
        intake = safe_json(todo.get("intake_data"))

        # Determine if this is a dependency repo sub-task
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        is_dep_workspace = bool(target_repo and target_repo.get("repo_url"))

        # Build workspace context if available
        workspace_context = ""
        if workspace_path:
            file_tree = self.workspace_mgr.get_file_tree(workspace_path, max_depth=4)

            if is_dep_workspace:
                dep_name = target_repo.get("name", "dependency")
                workspace_context = (
                    f"\n\nYou are working inside the DEPENDENCY repository: {dep_name}\n"
                    f"This is a dependency of the main project. Your changes will create a PR "
                    f"on this dependency's repo.\n"
                    f"Repository file structure:\n{file_tree}\n"
                )
                # Main repo available for reference
                main_repo_dir = os.path.join(workspace_path, "main_repo")
                if os.path.isdir(main_repo_dir):
                    workspace_context += (
                        "\nThe main project repo is available for reference (read-only) at "
                        "../main_repo/ relative to repo root.\n"
                        "Use read_file('../main_repo/path') to see how the main project "
                        "uses this dependency.\n"
                    )
            else:
                workspace_context = (
                    f"\n\nYou are working inside the project repository root directory.\n"
                    f"Project file structure:\n{file_tree}\n"
                )

            # List available dependency repos for reference
            task_deps_dir = os.path.join(workspace_path, "deps")
            if os.path.isdir(task_deps_dir):
                dep_entries = sorted(os.listdir(task_deps_dir))
                if dep_entries:
                    workspace_context += (
                        "\nDependency repositories available for reference (read-only):\n"
                        "Access via ../deps/{name}/path relative to repo root.\n"
                    )
                    for d in dep_entries:
                        full = os.path.join(task_deps_dir, d)
                        if os.path.isdir(full):
                            workspace_context += f"  - ../deps/{d}/\n"

        # Point agents to .context/ files instead of injecting inline.
        cross_repo_ctx = ""
        context_dir = os.path.join(workspace_path, ".context") if workspace_path else None
        if context_dir and os.path.isdir(context_dir):
            ctx_files = []
            if os.path.isfile(os.path.join(context_dir, "UNDERSTANDING.md")):
                ctx_files.append("- ../.context/UNDERSTANDING.md \u2014 main repo architecture, patterns, tech stack")
            if os.path.isfile(os.path.join(context_dir, "LINKING.md")):
                ctx_files.append("- ../.context/LINKING.md \u2014 how repos relate, data flow, integration patterns")
            deps_ctx = os.path.join(context_dir, "deps")
            if os.path.isdir(deps_ctx):
                dep_files = sorted(f for f in os.listdir(deps_ctx) if f.endswith(".md"))
                for df in dep_files:
                    name = df.removesuffix(".md")
                    ctx_files.append(f"- ../.context/deps/{df} \u2014 {name} dependency architecture and API")
            if ctx_files:
                cross_repo_ctx = (
                    "\n\nProject context docs available (read with read_file when needed):\n"
                    + "\n".join(ctx_files)
                    + "\n"
                )

        # Work rules injection
        rules_block = ""
        if work_rules:
            filtered = self.filter_rules_for_role(work_rules, role)
            rules_block = self.format_rules_for_prompt(filtered)

        # Use custom agent prompt if available, otherwise default
        if agent_config and agent_config.get("system_prompt"):
            system = agent_config["system_prompt"] + workspace_context + cross_repo_ctx
        else:
            system = get_default_system_prompt(role) + workspace_context + cross_repo_ctx

        system += rules_block

        # For debugger agents, inject project-level debug context
        if role == "debugger":
            debug_block = await self.build_debug_context_block(todo)
            if debug_block:
                system += debug_block

        # For tester agents, inject project build/test commands
        if role == "tester":
            build_block = await self.build_tester_context(todo)
            if build_block:
                system += build_block

        # Inject tool descriptions into prompt so all models know what's available
        if workspace_path:
            system += build_tools_prompt_block(role)

        from agents.orchestrator.output_validator import build_structured_output_instruction
        system += build_structured_output_instruction(role)

        prev_context = ""
        if previous_results:
            prev_items = []
            for r in previous_results:
                out = r.get("output_result", {})
                summary = out.get("approach", "") or out.get("summary", "") if isinstance(out, dict) else ""
                prev_items.append(
                    f"- [{r.get('agent_role', '?')}] {r.get('title', '?')}: {summary[:600]}"
                )
            prev_context = "\n\nCompleted sub-tasks:\n" + "\n".join(prev_items)

        # For debugger: extract structured error details from previous subtasks
        debugger_error_context = ""
        if role == "debugger" and previous_results:
            error_details = []
            for r in previous_results:
                out = r.get("output_result", {})
                if not isinstance(out, dict):
                    continue
                for f in (out.get("failures") or [])[:10]:
                    error_details.append(
                        f"- [{f.get('type','test')}] {f.get('file','?')}: {f.get('error','')}\n"
                        f"  {f.get('details','')[:300]}"
                    )
                for iss in out.get("issues") or []:
                    if iss.get("severity") in ("critical", "major"):
                        error_details.append(
                            f"- [review:{iss['severity']}] {iss.get('file','')}:{iss.get('line','')}: "
                            f"{iss.get('description','')}"
                        )
            if error_details:
                debugger_error_context = (
                    "\n## Errors to Investigate\n"
                    "These failures were found by previous agents. Start your investigation here:\n"
                    + "\n".join(error_details)
                )

        # For debugger: inject recent git log
        debugger_git_context = ""
        if role == "debugger" and workspace_path:
            repo_dir = os.path.join(workspace_path, "repo")
            if os.path.isdir(repo_dir):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "log", "--oneline", "--no-decorate", "-20",
                        cwd=repo_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    git_log = stdout.decode(errors="replace").strip()
                    if git_log:
                        debugger_git_context = (
                            "\n## Recent Commits\n"
                            "Scan for commits related to the bug area. Use "
                            "`run_command` with `git diff <hash>~1..<hash>` to inspect suspicious ones.\n"
                            f"```\n{git_log}\n```"
                        )
                except Exception:
                    pass

        # Inject previous run context if this is a retry-with-context
        previous_run = intake.get("previous_run") if intake and not isinstance(intake, str) else None
        if previous_run:
            prev_run_ctx = f"\n\nRETRY \u2014 previous outcome: {previous_run.get('previous_state', 'unknown')}\n"
            if previous_run.get("result_summary"):
                prev_run_ctx += f"{previous_run['result_summary']}\n"
            for pst in previous_run.get("sub_tasks", []):
                prev_run_ctx += f"- [{pst.get('role', '?')}] {pst.get('title', '?')}: {pst.get('status', '?')}"
                if pst.get("error"):
                    prev_run_ctx += f" (error: {pst['error']})"
                prev_run_ctx += "\n"
            prev_context += prev_run_ctx

        # Build structured user prompt with all available context
        user_parts = [
            f"# Task: {todo['title']}",
        ]
        if todo.get("description"):
            user_parts.append(f"**Task description:** {todo['description']}")

        user_parts.append(f"\n## Your Sub-task: {sub_task['title']}")
        user_parts.append(f"**Instructions:** {sub_task['description'] or 'N/A'}")

        # Inject planner's exploration context (input_context from DB)
        input_ctx = sub_task.get("input_context")
        if isinstance(input_ctx, str):
            try:
                input_ctx = json.loads(input_ctx)
            except (json.JSONDecodeError, TypeError):
                input_ctx = None
        if input_ctx and isinstance(input_ctx, dict):
            ctx_parts = []
            if input_ctx.get("relevant_files"):
                files = input_ctx["relevant_files"]
                if isinstance(files, list):
                    ctx_parts.append("**Relevant files:** " + ", ".join(f"`{f}`" for f in files))
            if input_ctx.get("current_state"):
                ctx_parts.append(f"**Current state:** {input_ctx['current_state']}")
            if input_ctx.get("what_to_change"):
                ctx_parts.append(f"**What to change:** {input_ctx['what_to_change']}")
            if input_ctx.get("patterns_to_follow"):
                ctx_parts.append(f"**Patterns to follow:** {input_ctx['patterns_to_follow']}")
            if input_ctx.get("related_code"):
                ctx_parts.append(f"**Related code:**\n{input_ctx['related_code']}")
            if input_ctx.get("integration_points"):
                ctx_parts.append(f"**Integration points:** {input_ctx['integration_points']}")
            if ctx_parts:
                user_parts.append("\n## Exploration Context (from planning)")
                user_parts.extend(ctx_parts)

        # Explicit workspace info
        if is_dep_workspace and target_repo:
            dep_name = target_repo.get("name", "dependency")
            user_parts.append(
                f"\n## Workspace: dependency repo `{dep_name}`"
                f"\nYou are editing the `{dep_name}` dependency repo. "
                f"Your changes will create a PR on this repo's repository."
                f"\nMain project is available read-only at `../main_repo/`."
            )
        elif workspace_path:
            user_parts.append("\n## Workspace: main project repo")

        if intake and not isinstance(intake, str):
            # Only include intake if it has meaningful content beyond previous_run
            clean_intake = {k: v for k, v in intake.items() if k != "previous_run" and v}
            if clean_intake:
                user_parts.append(f"\n**Requirements:** {json.dumps(clean_intake, default=str)}")

        if prev_context:
            user_parts.append(prev_context)
        if debugger_error_context:
            user_parts.append(debugger_error_context)
        if debugger_git_context:
            user_parts.append(debugger_git_context)

        return {
            "system": system,
            "user": "\n".join(user_parts),
        }

    # ------------------------------------------------------------------
    # Iteration context (RALPH-style loops)
    # ------------------------------------------------------------------

    async def build_iteration_context(
        self,
        sub_task: dict,
        iteration: int,
        iteration_log: list[dict],
        workspace_path: str | None,
        work_rules: dict,
        agent_config: dict | None = None,
        cached_repo_map: str | None = None,
    ) -> tuple[dict, str | None]:
        """Build completely fresh context for one RALPH iteration.

        Returns ``(context_dict, repo_map_text)`` \u2014 *repo_map_text* is
        returned so callers can cache it across iterations instead of
        regenerating.
        """
        todo = await self.load_todo()
        previous_results = await self.get_completed_results_cached()
        intake = safe_json(todo.get("intake_data"))

        # Determine if this is a dependency repo sub-task
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        is_dep_workspace = bool(target_repo and target_repo.get("repo_url"))

        # Workspace context
        workspace_context = ""
        if workspace_path:
            file_tree = self._get_file_tree_cached(workspace_path)

            if is_dep_workspace:
                dep_name = target_repo.get("name", "dependency")
                workspace_context = (
                    f"\n\nYou are working inside the DEPENDENCY repository: {dep_name}\n"
                    f"Your changes will create a PR on this dependency's repo.\n"
                    f"Repository file structure:\n{file_tree}\n"
                )
                main_repo_dir = os.path.join(workspace_path, "main_repo")
                if os.path.isdir(main_repo_dir):
                    workspace_context += (
                        "\nThe main project repo is available for reference (read-only) at "
                        "../main_repo/ relative to repo root.\n"
                    )
            else:
                workspace_context = (
                    f"\n\nYou are working inside the project repository root directory.\n"
                    f"Project file structure:\n{file_tree}\n"
                )

            # List available dependency repos
            task_deps_dir = os.path.join(workspace_path, "deps")
            if os.path.isdir(task_deps_dir):
                dep_entries = [d for d in sorted(os.listdir(task_deps_dir))
                               if os.path.isdir(os.path.join(task_deps_dir, d))]
                if dep_entries:
                    workspace_context += (
                        "\nDependency repos available (read-only) via ../deps/{name}/path:\n"
                    )
                    for d in dep_entries:
                        workspace_context += f"  - ../deps/{d}/\n"

            # Git diff of current changes
            try:
                repo_dir = os.path.join(workspace_path, "repo")
                if os.path.isdir(repo_dir):
                    proc = await asyncio.create_subprocess_exec(
                        "git", "diff", "--stat",
                        cwd=repo_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    diff_stat = stdout.decode(errors="replace").strip()
                    if diff_stat:
                        workspace_context += f"\nCurrent changes (git diff --stat):\n{diff_stat}\n"
            except Exception:
                pass

        # Work rules block
        rules_block = self.format_rules_for_prompt(work_rules)

        # System prompt — use custom agent config if available
        role = sub_task["agent_role"]
        if agent_config and agent_config.get("system_prompt"):
            system = agent_config["system_prompt"]
        else:
            system = get_default_system_prompt(role)
        system += workspace_context
        system += rules_block

        # For debugger agents, inject debug context
        if role == "debugger":
            debug_block = await self.build_debug_context_block(todo)
            if debug_block:
                system += debug_block

        # Inject tool descriptions for models that need explicit instructions
        if workspace_path:
            system += build_tools_prompt_block(role)

        from agents.orchestrator.output_validator import build_structured_output_instruction
        system += build_structured_output_instruction(role)

        # Completion instruction
        system += (
            "\n\n## IMPORTANT: Signaling Completion\n"
            "When you have finished ALL your work (code written, tests passing, etc.), "
            "you MUST call the `task_complete` tool with a summary of what you accomplished. "
            "This stops the iteration loop. Do NOT keep working after you are done."
        )

        # -- Repo Map (tree-sitter + PageRank) -- cached across iterations --
        repo_map_text = cached_repo_map
        if workspace_path and repo_map_text is None:
            try:
                from agents.indexing.indexer import RepoIndexer
                from agents.indexing.repo_map import render_repo_map
                from agents.utils.token_counter import count_tokens

                repo_dir_for_map = os.path.join(workspace_path, "repo")
                if os.path.isdir(repo_dir_for_map):
                    _subtask_idx = os.path.join(workspace_path, ".agent_index")
                    indexer = RepoIndexer()
                    graph = indexer.index(repo_dir_for_map, cache_dir=_subtask_idx)
                    if graph.symbol_count > 0:
                        repo_map_budget = settings.repo_map_token_budget
                        repo_map_text = render_repo_map(
                            graph,
                            token_budget=repo_map_budget,
                            count_tokens_fn=lambda t: count_tokens(t, "default"),
                        )
                        logger.info(
                            "[%s] Generated repo map: %d symbols, %d files",
                            self.todo_id[:8], graph.symbol_count, graph.file_count,
                        )
            except ImportError:
                logger.debug("[%s] tree-sitter indexing not available", self.todo_id[:8])
            except Exception:
                logger.debug("[%s] Repo map generation failed", self.todo_id[:8], exc_info=True)
        if repo_map_text:
            system += f"\n\n## Repository Symbol Map\n{repo_map_text}\n"

        # -- Project Memories (cached across iterations) --
        memories_rows = await self._get_memories_cached(str(todo["project_id"]))
        if memories_rows:
            memories_block = "\n\n## Project Memories (learnings from past tasks)\n"
            for m in memories_rows:
                memories_block += f"- [{m['category']}] {m['content']}\n"
            system += memories_block

        # -- Iteration Learnings (with LLM compaction for old entries) --
        if iteration_log:
            try:
                from agents.utils.context_compaction import compact_iteration_log, format_compacted_entry
                provider = await self.provider_registry.resolve_for_todo(self.todo_id)
                compacted_log = await compact_iteration_log(
                    iteration_log, provider,
                    keep_recent=settings.context_compaction_keep_recent,
                )
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in compacted_log:
                    if "_compacted" in entry:
                        learnings_block += format_compacted_entry(entry) + "\n"
                    else:
                        status = "PASSED" if entry.get("outcome") == "passed" else f"FAILED ({entry.get('outcome', '?')})"
                        learnings_block += f"- Iteration {entry.get('iteration', '?')}: {status}"
                        if entry.get("learnings"):
                            learnings_block += " \u2014 " + "; ".join(entry["learnings"])
                        learnings_block += "\n"
                        if entry.get("error_output"):
                            err = entry["error_output"][:500]
                            learnings_block += f"  Error: {err}\n"
                system += learnings_block
            except ImportError:
                # Fallback: original behavior without compaction
                recent = iteration_log[-5:]
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in recent:
                    status = "PASSED" if entry["outcome"] == "passed" else f"FAILED ({entry['outcome']})"
                    learnings_block += f"- Iteration {entry['iteration']}: {status}"
                    if entry.get("learnings"):
                        learnings_block += " \u2014 " + "; ".join(entry["learnings"])
                    learnings_block += "\n"
                    if entry.get("error_output"):
                        err = entry["error_output"]
                        if len(err) > 1500:
                            err = err[:1500] + f"\n... ({len(entry['error_output']) - 1500} more chars — run the failing command to see full output)"
                        learnings_block += f"  Error: {err}\n"
                system += learnings_block
            except Exception:
                logger.debug("[%s] Context compaction failed, using raw log", self.todo_id[:8], exc_info=True)
                recent = iteration_log[-5:]
                learnings_block = "\n\n## Previous Iteration Learnings\n"
                for entry in recent:
                    status = "PASSED" if entry.get("outcome") == "passed" else f"FAILED ({entry.get('outcome', '?')})"
                    learnings_block += f"- Iteration {entry.get('iteration', '?')}: {status}"
                    if entry.get("learnings"):
                        learnings_block += " \u2014 " + "; ".join(entry["learnings"])
                    learnings_block += "\n"
                    if entry.get("error_output"):
                        err = entry["error_output"]
                        if len(err) > 1500:
                            err = err[:1500] + f"\n... ({len(entry['error_output']) - 1500} more chars — run the failing command to see full output)"
                        learnings_block += f"  Error: {err}\n"
                system += learnings_block

        # Previous results context (truncate to save tokens)
        prev_context = ""
        if previous_results:
            prev_items = []
            for r in previous_results:
                out = r.get("output_result", {})
                summary = out.get("approach", "") or out.get("summary", "") if isinstance(out, dict) else ""
                prev_items.append(
                    f"- [{r.get('agent_role', '?')}] {r.get('title', '?')}: {summary[:600]}"
                )
            prev_context = "\n\nCompleted sub-tasks:\n" + "\n".join(prev_items)

        # For debugger: extract structured error details from previous subtasks
        debugger_error_context = ""
        if role == "debugger" and previous_results:
            error_details = []
            for r in previous_results:
                out = r.get("output_result", {})
                if not isinstance(out, dict):
                    continue
                for f in (out.get("failures") or [])[:10]:
                    error_details.append(
                        f"- [{f.get('type','test')}] {f.get('file','?')}: {f.get('error','')}\n"
                        f"  {f.get('details','')[:300]}"
                    )
                for iss in out.get("issues") or []:
                    if iss.get("severity") in ("critical", "major"):
                        error_details.append(
                            f"- [review:{iss['severity']}] {iss.get('file','')}:{iss.get('line','')}: "
                            f"{iss.get('description','')}"
                        )
            if error_details:
                debugger_error_context = (
                    "\n## Errors to Investigate\n"
                    "These failures were found by previous agents. Start your investigation here:\n"
                    + "\n".join(error_details)
                )

        # For debugger: inject recent git log
        debugger_git_context = ""
        if role == "debugger" and workspace_path:
            repo_dir = os.path.join(workspace_path, "repo")
            if os.path.isdir(repo_dir):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "log", "--oneline", "--no-decorate", "-20",
                        cwd=repo_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    git_log = stdout.decode(errors="replace").strip()
                    if git_log:
                        debugger_git_context = (
                            "\n## Recent Commits\n"
                            "Scan for commits related to the bug area. Use "
                            "`run_command` with `git diff <hash>~1..<hash>` to inspect suspicious ones.\n"
                            f"```\n{git_log}\n```"
                        )
                except Exception:
                    pass

        # Build structured user prompt with exploration context
        user_parts = [
            f"# Task: {todo['title']}",
        ]
        if todo.get("description"):
            user_parts.append(f"**Task description:** {todo['description']}")

        user_parts.append(f"\n## Your Sub-task: {sub_task['title']}")
        user_parts.append(f"**Instructions:** {sub_task['description'] or 'N/A'}")
        user_parts.append(f"**Iteration:** {iteration}")

        # Inject planner's exploration context (input_context from DB)
        input_ctx = sub_task.get("input_context")
        if isinstance(input_ctx, str):
            try:
                input_ctx = json.loads(input_ctx)
            except (json.JSONDecodeError, TypeError):
                input_ctx = None
        if input_ctx and isinstance(input_ctx, dict):
            ctx_parts = []
            if input_ctx.get("relevant_files"):
                files = input_ctx["relevant_files"]
                if isinstance(files, list):
                    ctx_parts.append("**Relevant files:** " + ", ".join(f"`{f}`" for f in files))
            if input_ctx.get("current_state"):
                ctx_parts.append(f"**Current state:** {input_ctx['current_state']}")
            if input_ctx.get("what_to_change"):
                ctx_parts.append(f"**What to change:** {input_ctx['what_to_change']}")
            if input_ctx.get("patterns_to_follow"):
                ctx_parts.append(f"**Patterns to follow:** {input_ctx['patterns_to_follow']}")
            if input_ctx.get("related_code"):
                ctx_parts.append(f"**Related code:**\n{input_ctx['related_code']}")
            if input_ctx.get("integration_points"):
                ctx_parts.append(f"**Integration points:** {input_ctx['integration_points']}")
            if ctx_parts:
                user_parts.append("\n## Exploration Context (from planning)")
                user_parts.extend(ctx_parts)

        # Explicit workspace info
        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            try:
                target_repo = json.loads(target_repo)
            except (json.JSONDecodeError, TypeError):
                target_repo = None
        if target_repo and target_repo.get("repo_url"):
            dep_name = target_repo.get("name", "dependency")
            user_parts.append(
                f"\n## Workspace: dependency repo `{dep_name}`"
                f"\nYou are editing the `{dep_name}` dependency repo."
            )
        elif workspace_path:
            user_parts.append("\n## Workspace: main project repo")

        if intake and not isinstance(intake, str):
            clean_intake = {k: v for k, v in intake.items() if k != "previous_run" and v}
            if clean_intake:
                user_parts.append(f"\n**Requirements:** {json.dumps(clean_intake, default=str)}")

        if prev_context:
            user_parts.append(prev_context)
        if debugger_error_context:
            user_parts.append(debugger_error_context)
        if debugger_git_context:
            user_parts.append(debugger_git_context)

        user_content = "\n".join(user_parts)

        return {"system": system, "user": user_content}, repo_map_text

    # ------------------------------------------------------------------
    # Debug context block
    # ------------------------------------------------------------------

    async def build_debug_context_block(self, todo: dict) -> str:
        """Build a markdown block with debug context for debugger agents.

        Pulls log sources, MCP data hints, and custom instructions from
        the project's ``settings_json.debug_context``. Also checks
        dependency-level debug contexts from ``context_docs``.
        """
        project = await self.db.fetchrow(
            "SELECT settings_json, context_docs FROM projects WHERE id = $1",
            todo["project_id"],
        )
        if not project:
            return ""

        proj_settings = project.get("settings_json") or {}
        if isinstance(proj_settings, str):
            proj_settings = json.loads(proj_settings)
        debug_ctx = proj_settings.get("debug_context") or {}

        parts: list[str] = []

        # Log sources
        log_sources = debug_ctx.get("log_sources") or []
        if log_sources:
            parts.append("\n\n## Log Sources")
            for src in log_sources:
                parts.append(f"### {src.get('service_name', 'Service')}")
                if src.get("log_path"):
                    parts.append(f"- **Log path:** `{src['log_path']}`")
                if src.get("log_command"):
                    parts.append(f"- **Log command:** `{src['log_command']}`")
                if src.get("description"):
                    parts.append(f"- **Description:** {src['description']}")

        # MCP data hints
        mcp_hints = debug_ctx.get("mcp_hints") or []
        if mcp_hints:
            parts.append("\n\n## MCP Data Sources")
            for hint in mcp_hints:
                name = hint.get("mcp_server_name", "MCP Server")
                parts.append(f"### {name}")
                available = hint.get("available_data") or []
                if available:
                    parts.append("**Available data:** " + ", ".join(available))
                queries = hint.get("example_queries") or []
                if queries:
                    parts.append("**Example queries:**")
                    for q in queries:
                        parts.append(f"  ```\n  {q}\n  ```")
                if hint.get("notes"):
                    parts.append(f"**Notes:** {hint['notes']}")

        # Custom instructions
        custom = debug_ctx.get("custom_instructions") or ""
        if custom:
            parts.append(f"\n\n## Debug Instructions\n{custom}")

        # Dependency-level debug contexts
        deps = project.get("context_docs") or []
        if isinstance(deps, str):
            deps = json.loads(deps)
        for dep in deps:
            dep_debug = dep.get("debug_context") if isinstance(dep, dict) else None
            if dep_debug:
                dep_name = dep.get("name", "Dependency")
                parts.append(f"\n\n## Debug Context: {dep_name}")
                for src in dep_debug.get("log_sources", []):
                    if src.get("log_path"):
                        parts.append(f"- Log: `{src['log_path']}`")
                    if src.get("log_command"):
                        parts.append(f"- Command: `{src['log_command']}`")
                for hint in dep_debug.get("mcp_hints", []):
                    parts.append(f"- MCP: {hint.get('mcp_server_name', '?')} \u2014 {', '.join(hint.get('available_data', []))}")
                if dep_debug.get("custom_instructions"):
                    parts.append(f"- Instructions: {dep_debug['custom_instructions']}")

        if not parts:
            parts.append(
                "\n\n## Debug Context\n"
                "No debug context is configured for this project. "
                "Use codebase exploration, error messages, and available MCP tools "
                "to investigate the issue."
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Tester context block
    # ------------------------------------------------------------------

    async def build_tester_context(self, todo: dict) -> str:
        """Build a context block with build/test commands for tester agents.

        If the project has configured ``build_commands``, inject them.
        Otherwise, provide discovery instructions.
        """
        project = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1",
            todo["project_id"],
        )
        proj_settings = safe_json(project.get("settings_json")) if project else {}
        build_commands = proj_settings.get("build_commands", [])

        if build_commands:
            cmds = "\n".join(f"  - `{cmd}`" for cmd in build_commands)
            return (
                f"\n\n## Project Build & Test Commands\n"
                f"The project has these configured build/test commands:\n{cmds}\n"
                f"You MUST run these commands to validate the implementation. "
                f"If any fail, investigate and fix the issues before reporting success."
            )
        else:
            return (
                "\n\n## Build & Test Discovery\n"
                "No build/test commands are configured for this project. "
                "Discover them by checking:\n"
                "- `package.json` \u2192 `scripts.test`, `scripts.build`, `scripts.lint`\n"
                "- `Makefile` or `Taskfile.yml`\n"
                "- `pyproject.toml` / `setup.py` / `tox.ini`\n"
                "- `Cargo.toml` (cargo test)\n"
                "- CI config files (`.github/workflows/`, `.gitlab-ci.yml`)\n\n"
                "Run the discovered test/build commands to validate the implementation."
            )
