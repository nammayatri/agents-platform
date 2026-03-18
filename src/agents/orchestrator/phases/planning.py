"""Planning phase — explore codebase and decompose task into sub-tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from agents.agents.registry import build_tools_prompt_block
from agents.config.settings import settings
from agents.schemas.agent import LLMMessage
from agents.utils.json_helpers import parse_llm_json, safe_json

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """\
You are a senior project planner. Your job is to explore the codebase, understand the \
existing architecture, and decompose the task into well-structured sub-tasks for specialist agents.

## Workflow
1. FIRST, use the workspace tools (read_file, list_directory, search_files, run_command) \
to explore the codebase. Understand the project structure, relevant files, existing patterns, \
and conventions BEFORE creating a plan.
2. Read relevant source files to understand what already exists and what needs to change.
3. Only AFTER exploring, output your execution plan as a JSON object.

## Planning Rules

CRITICAL — MAXIMIZE PARALLELISM:
- You have MULTIPLE coding agents that execute concurrently. Your #1 job is to split the \
work so as many agents can run IN PARALLEL as possible.
- Decompose the work into focused, INDEPENDENT sub-tasks. Each sub-task should touch a \
DIFFERENT set of files or a DIFFERENT concern so agents don't conflict.
- Sub-tasks at the same execution_order with no depends_on run concurrently on separate agents.
- Use depends_on (0-based sub-task indexes) ONLY when there is a true data dependency \
(e.g. task B reads a file that task A creates). Do NOT add depends_on for loose coupling.

PARALLELISM STRATEGIES:
- **Split by file/module**: If 5 files need changes, create 5 parallel coder sub-tasks, one per file.
- **Split by layer**: Frontend and backend changes can always run in parallel.
- **Split by feature**: Independent features or components should be separate parallel tasks.
- **Shared types/interfaces first**: If multiple tasks need a shared type, create one small \
sub-task for the shared types (execution_order=0), then all consumers depend on it and run \
in parallel (execution_order=1).
- **Aim for 3-8 parallel coder sub-tasks** for medium tasks, more for larger ones. \
A plan with only 1-2 sequential coder sub-tasks is almost always wrong — decompose further.

BAD PLAN (sequential, slow):
  sub_task 0: "Create backend API" (order=0)
  sub_task 1: "Create frontend page" (order=1, depends_on=[0])
  sub_task 2: "Write tests" (order=2, depends_on=[1])

GOOD PLAN (parallel, fast):
  sub_task 0: "Create shared types and interfaces" (order=0)
  sub_task 1: "Create backend API endpoint" (order=1, depends_on=[0])
  sub_task 2: "Create frontend API client" (order=1, depends_on=[0])
  sub_task 3: "Create frontend page component" (order=1, depends_on=[0])
  sub_task 4: "Write backend tests" (order=1, depends_on=[0])
  sub_task 5: "Write frontend tests" (order=1, depends_on=[0])

AGENT ROLES:
- **coder** — Implements code, fixes bugs, adds features. Use for all code changes.
- **debugger** — Investigates and fixes bugs using logs, metrics, database queries, and VM access. \
Use for bug reports, error investigations, production incidents, and performance issues.
- **tester** — Writes and runs tests. Use after coder sub-tasks to validate changes.
- **reviewer** — Reviews code quality, checks for bugs/security. Use for important changes.
- **report_writer** — Generates documentation and reports.

NOTE: Do NOT create pr_creator or merge_agent sub-tasks in your plan. \
PR creation and merging are handled automatically by the system after coder work completes.

REVIEW LOOP (review_loop field):
- Set review_loop=true for critical or complex code changes that need the full \
coder→reviewer→PR→merge cycle. The system will automatically chain a reviewer, PR creator, \
and merge agent after the coder completes.
- Use review_loop=true for: core business logic, security-sensitive code, API changes, \
database migrations, infrastructure changes.
- Use review_loop=false for: simple fixes, config changes, documentation, test-only changes, \
straightforward additions.

SUB-TASK DESCRIPTIONS:
- Be specific and detailed. Include which files to modify, what patterns to follow, \
and what the expected outcome is.
- Reference actual file paths and code patterns you discovered during exploration.
- Include relevant context from your codebase exploration so the agent doesn't need to \
re-discover everything.

SUB-TASK CONTEXT (the "context" field — REQUIRED for coder/debugger sub-tasks):
Populate the "context" object on each sub-task with your exploration findings:
- relevant_files: list of file paths the agent should look at or modify
- current_state: what the code currently does (so the agent knows the starting point)
- what_to_change: specific changes needed (not just "fix the bug" — describe the fix)
- patterns_to_follow: coding patterns, naming conventions, or examples from the codebase
- related_code: brief snippets or function signatures discovered during exploration
- integration_points: how this sub-task connects to other sub-tasks or repos

## Cross-Repo Exploration

Dependency repos are available at ../deps/{name}/ (read-only via workspace tools).
- Use list_directory(path="../deps/") to see which dependency repos are available.
- Use read_file(path="../deps/{name}/src/...") to read dependency source code.
- Use search_files(pattern="...", path="../deps/{name}/") to search within a dependency.
- When a task involves cross-repo concerns (shared types, API consumed from a dep, \
integration patterns), ALWAYS explore both the main repo AND the relevant deps first.
- For sub-tasks that need to modify a dependency repo, set target_repo to the \
dependency name string (e.g. "auth-service"). The orchestrator resolves all repo metadata.

## Query Enrichment

Before creating your plan, proactively enrich the user's request:
- Identify ambiguities or missing context in the task description.
- Explore the codebase to find the actual file paths, current implementation, and patterns.
- Discover the current behavior so you can describe what needs to change.
- Make each sub-task description self-contained with all discovered context \
(file paths, function names, current patterns) so agents can start working immediately.

## Output Format

After exploring the codebase, output ONLY a JSON object (no markdown fences, no extra text):
{"summary":"...", "sub_tasks":[{"title":"...", "description":"...", "agent_role":"...", \
"execution_order":0, "depends_on":[], "review_loop":false, "target_repo":"main", \
"context":{"relevant_files":[], "current_state":"...", "what_to_change":"...", \
"patterns_to_follow":"...", "related_code":"...", "integration_points":"..."}}], \
"estimated_tokens":5000}

Sub-task fields:
- title: short descriptive title
- description: detailed instructions for the agent
- agent_role: one of the roles above
- execution_order: 0 for parallel, sequential number for ordered execution
- depends_on: list of 0-based indexes of sub-tasks this depends on
- review_loop: true for critical code changes needing coder→reviewer→merge cycle
- target_repo: REQUIRED — every sub-task MUST have this field set. Use "main" for work in \
the main project repo. For dependency repo work, use the EXACT dependency name string from \
the dependency repos list below (e.g. "auth-service"). This routes the agent to the correct \
workspace — a wrong value means the agent codes in the wrong repo and the task fails.
- context: exploration findings for the agent (REQUIRED for coder/debugger). Include \
relevant_files, current_state, what_to_change, patterns_to_follow, related_code, \
integration_points. This is the agent's primary reference — make it thorough.
"""


class PlanningPhase:
    """Explore codebase with tools, then decompose task into sub-tasks."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, todo: dict, provider: AIProvider) -> None:
        """Explore codebase with tools, then decompose task into sub-tasks."""
        coord = self._coord

        if todo["state"] != "planning":
            await coord._transition_todo("planning", sub_state="decomposing")
        else:
            await coord.db.execute(
                "UPDATE todo_items SET sub_state = 'decomposing', updated_at = NOW() WHERE id = $1",
                coord.todo_id,
            )

        context = await coord._build_context(todo)
        intake_data = safe_json(todo.get("intake_data"))

        # Inject available custom agents into planner context
        custom_agents = await coord.db.fetch(
            "SELECT role, name, description FROM agent_configs "
            "WHERE owner_id = $1 AND is_active = TRUE",
            todo["creator_id"],
        )
        available_roles = "coder, debugger, tester, reviewer, report_writer"
        if custom_agents:
            custom_lines = [f"  - {a['role']}: {a['name']} — {a['description'] or 'no description'}" for a in custom_agents]
            available_roles += "\n\nCustom agents available:\n" + "\n".join(custom_lines)

        planner_prompt = PLANNER_SYSTEM_PROMPT + f"\n\nAvailable agent roles: {available_roles}\n"

        # Resolve workspace for codebase exploration
        workspace_path = None
        try:
            project = await coord.db.fetchrow(
                "SELECT repo_url, workspace_path FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if project and project.get("repo_url"):
                workspace_path = await coord.workspace_mgr.setup_task_workspace(coord.todo_id)
                logger.info("[%s] Planner workspace ready at %s", coord.todo_id, workspace_path)
        except Exception:
            logger.warning("[%s] Could not set up planner workspace", coord.todo_id, exc_info=True)

        # ── Pre-build code indexes (structural + embedding) ──
        repo_map_text: str | None = None
        if workspace_path:
            repo_dir = os.path.join(workspace_path, "repo")
            if os.path.isdir(repo_dir):
                project_index_dir = os.path.normpath(
                    os.path.join(workspace_path, "..", "..", ".agent_index")
                )
                task_index_dir = os.path.join(workspace_path, ".agent_index")
                try:
                    from agents.indexing import (
                        build_indexes_and_repo_map,
                        copy_project_index_to_task,
                    )

                    _had_base = await asyncio.to_thread(
                        copy_project_index_to_task, project_index_dir, task_index_dir,
                    )

                    await coord._post_system_message("Indexing codebase for planning...")
                    _idx_t0 = time.monotonic()
                    repo_map_text = await asyncio.to_thread(
                        build_indexes_and_repo_map,
                        repo_dir,
                        cache_dir=task_index_dir,
                        repo_map_budget=settings.repo_map_token_budget,
                    )
                    _idx_ms = int((time.monotonic() - _idx_t0) * 1000)
                    logger.info(
                        "[%s] Pre-indexing complete: repo_map=%s took=%dms",
                        coord.todo_id,
                        f"{len(repo_map_text)} chars" if repo_map_text else "None",
                        _idx_ms,
                    )
                    try:
                        await coord.redis.publish(
                            f"task:{coord.todo_id}:events",
                            json.dumps({
                                "type": "index_build",
                                "latency_ms": _idx_ms,
                                "has_repo_map": repo_map_text is not None,
                                "repo_map_chars": len(repo_map_text) if repo_map_text else 0,
                                "from_base": _had_base,
                            }),
                        )
                    except Exception:
                        pass
                except Exception:
                    logger.warning(
                        "[%s] Pre-indexing failed, planner will proceed without repo map",
                        coord.todo_id, exc_info=True,
                    )

        # Add workspace context and tool instructions to system prompt
        if workspace_path:
            file_tree = coord.workspace_mgr.get_file_tree(workspace_path, max_depth=3)
            workspace_block = f"\n\nProject file structure:\n{file_tree}\n"
            if repo_map_text:
                workspace_block += f"\n## Repository Symbol Map\n{repo_map_text}\n"
            workspace_block += (
                "\nYou MUST explore the codebase using the tools before outputting your plan. "
                "Read relevant files, search for patterns, and understand the existing code."
            )
            planner_prompt += workspace_block

            # Inject dependency info so planner knows about available deps
            dep_dirs = context.get("dependency_dirs", {})
            deps_list = context.get("dependencies", [])
            logger.info(
                "[%s] Planner dep injection: dep_dirs=%s, deps_list_count=%d",
                coord.todo_id, list(dep_dirs.keys()) if dep_dirs else "NONE", len(deps_list),
            )
            if dep_dirs:
                planner_prompt += "\n\nDependency repositories (read-only at ../deps/{dir_name}/, writable via target_repo):"
                planner_prompt += "\nTo modify a dep repo, set target_repo to the dep name string. The orchestrator resolves all metadata."
                for dep in deps_list:
                    name = dep.get("name", "")
                    dir_name = dep_dirs.get(name, "")
                    repo_url = dep.get("repo_url", "")
                    if dir_name:
                        planner_prompt += f"\n  - \"{name}\" (browse at ../deps/{dir_name}/)"
                        if not repo_url:
                            logger.warning(
                                "[%s] Dependency '%s' has no repo_url — target_repo will fail at execution",
                                coord.todo_id, name,
                            )

            # Inject cross-repo integration links from project understanding
            understanding = context.get("project_understanding", {})
            cross_links = understanding.get("cross_repo_links", []) if isinstance(understanding, dict) else []
            if cross_links:
                planner_prompt += "\n\nKnown cross-repo integration points:"
                for link in cross_links:
                    dep = link.get("dep_name", "?")
                    pattern = link.get("integration_pattern", "")
                    main_files = ", ".join(link.get("main_repo_files", [])[:5])
                    interfaces = ", ".join(link.get("shared_interfaces", [])[:5])
                    planner_prompt += f"\n  - {dep}: {pattern}"
                    if main_files:
                        planner_prompt += f" (main repo: {main_files})"
                    if interfaces:
                        planner_prompt += f" (interfaces: {interfaces})"

            # Inject per-dependency understandings
            dep_understandings = context.get("dep_understandings", {})
            if dep_understandings and isinstance(dep_understandings, dict):
                planner_prompt += "\n\nDependency repo understandings:"
                for dep_name, dep_u in dep_understandings.items():
                    if isinstance(dep_u, dict):
                        purpose = dep_u.get("purpose", "")
                        api = dep_u.get("api_surface", "")
                        planner_prompt += f"\n  - {dep_name}: {purpose}"
                        if api:
                            planner_prompt += f"\n    API: {api[:300]}"

            # Inject linking document overview
            linking_doc = context.get("linking_document", {})
            if linking_doc and isinstance(linking_doc, dict):
                overview = linking_doc.get("overview", "")
                if overview:
                    planner_prompt += f"\n\nCross-repo architecture:\n{overview[:800]}"

            planner_prompt += build_tools_prompt_block("planner")

        # Build user message — add explicit retry/diff context if present
        user_parts = [
            f"Task: {todo['title']}",
            f"Description: {todo['description'] or 'N/A'}",
            f"Type: {todo['task_type']}",
        ]

        # Add available dep repos to user message
        dep_dirs = context.get("dependency_dirs", {})
        if dep_dirs:
            dep_names_str = ", ".join(f"'{n}'" for n in dep_dirs.keys())
            user_parts.append(
                f"\nAvailable dependency repos: {dep_names_str}. "
                "If this task requires changes to any dependency repo, set target_repo to the "
                "dependency name on those sub-tasks (not 'main'). Explore the dep repos at "
                "../deps/{name}/ to understand their structure."
            )

        previous_run = intake_data.get("previous_run") if intake_data else None
        if previous_run:
            user_parts.append("\n## RETRY — Previous Run Context")
            user_parts.append(f"Previous state: {previous_run.get('previous_state', 'unknown')}")
            if previous_run.get("result_summary"):
                user_parts.append(f"Result summary: {previous_run['result_summary']}")

            prev_tasks = previous_run.get("sub_tasks", [])
            if prev_tasks:
                user_parts.append("\nPrevious sub-tasks:")
                for pst in prev_tasks:
                    status_icon = "✓" if pst["status"] == "completed" else "✗" if pst["status"] == "failed" else "○"
                    user_parts.append(f"  {status_icon} [{pst['role']}] {pst['title']} — {pst['status']}")
                    if pst.get("error"):
                        user_parts.append(f"    Error: {pst['error'][:300]}")

            git_diff = previous_run.get("git_diff")
            if git_diff:
                user_parts.append(f"\n## Existing Code Changes — main repo (git diff)\n{git_diff.get('stat', '')}")
                if git_diff.get("files"):
                    user_parts.append("\nChanged files:")
                    for f in git_diff["files"]:
                        user_parts.append(f"  {f['status']}\t{f['path']}")
                if git_diff.get("diff"):
                    user_parts.append(f"\nFull diff:\n```\n{git_diff['diff']}\n```")

            # Include diffs from dependency workspaces
            dep_diffs = previous_run.get("dep_diffs")
            if dep_diffs:
                for dep_name_key, dep_diff in dep_diffs.items():
                    user_parts.append(f"\n## Existing Code Changes — dependency `{dep_name_key}` (git diff)")
                    user_parts.append(dep_diff.get("stat", ""))
                    if dep_diff.get("files"):
                        user_parts.append("\nChanged files:")
                        for f in dep_diff["files"]:
                            user_parts.append(f"  {f['status']}\t{f['path']}")
                    if dep_diff.get("diff"):
                        user_parts.append(f"\nFull diff:\n```\n{dep_diff['diff']}\n```")

            if git_diff or dep_diffs:
                user_parts.append(
                    "\nIMPORTANT: The above code changes already exist in the workspace. "
                    "Create sub-tasks that build on or fix these existing changes — "
                    "do NOT start from scratch. Focus on what still needs to be done."
                )

            # Strip previous_run from intake_data to avoid duplication
            intake_for_prompt = {k: v for k, v in intake_data.items() if k != "previous_run"}
        else:
            intake_for_prompt = intake_data

        user_parts.append(f"\nIntake data: {json.dumps(intake_for_prompt, default=str)}")
        user_parts.append(f"Project context: {json.dumps(context, default=str)}")
        user_parts.append(
            "\nExplore the codebase first, then output your execution plan as a JSON object. "
            "The JSON must be the LAST thing you output — after all tool calls and exploration."
        )

        messages = [
            LLMMessage(role="system", content=planner_prompt),
            LLMMessage(role="user", content="\n".join(user_parts)),
        ]

        # Set up workspace tools for codebase exploration during planning
        planner_tools = None
        if workspace_path:
            planner_tools = coord._get_builtin_tools(workspace_path, "planner")
            _plan_idx = os.path.join(workspace_path, ".agent_index")
            _plan_dep_dirs = coord._get_dep_index_dirs(workspace_path)
            for _bt in planner_tools:
                if _bt["name"] == "semantic_search":
                    _bt["_index_dir"] = _plan_idx
                    if _plan_dep_dirs:
                        _bt["_dep_index_dirs"] = _plan_dep_dirs

            # Also include MCP tools if configured
            mcp_tools = await coord.tools_registry.resolve_tools(
                project_id=str(todo["project_id"]),
                user_id=str(todo["creator_id"]),
            )
            if mcp_tools:
                existing_names = {t["name"] for t in planner_tools}
                planner_tools.extend(t for t in mcp_tools if t["name"] not in existing_names)

        # Build submit_result tool for structured plan output
        from agents.orchestrator.structured_output import build_submit_tool, extract_submit_result

        # Build target_repo description with actual available dep names
        dep_names = list(context.get("dependency_dirs", {}).keys())
        if dep_names:
            allowed_values = ", ".join(f"'{n}'" for n in dep_names)
            target_repo_desc = (
                f"REQUIRED. Use 'main' for main-repo work. "
                f"Available dependency repos: {allowed_values}. "
                f"Set to the EXACT dependency name for sub-tasks that modify a dependency repo."
            )
        else:
            target_repo_desc = "REQUIRED. 'main' for main-repo work, or the dependency name for dep-repo work."

        plan_schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief plan summary"},
                "sub_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "agent_role": {"type": "string"},
                            "execution_order": {"type": "integer"},
                            "depends_on": {"type": "array", "items": {"type": "integer"}},
                            "review_loop": {"type": "boolean"},
                            "target_repo": {
                                "type": "string",
                                "description": target_repo_desc,
                            },
                            "context": {
                                "type": "object",
                                "description": "Exploration findings for the agent. REQUIRED for coder/debugger roles.",
                            },
                        },
                        "required": ["title", "description", "agent_role", "target_repo"],
                    },
                },
                "estimated_tokens": {"type": "integer"},
            },
            "required": ["summary", "sub_tasks"],
        }
        plan_submit_tool = build_submit_tool(plan_schema, "plan",
            "Submit your execution plan. Call this AFTER exploring the codebase.")

        plan = None
        max_retries = 3
        for attempt in range(max_retries):
            if planner_tools:
                from agents.providers.base import run_tool_loop

                tools_with_submit = list(planner_tools) + [plan_submit_tool]

                async def _plan_tool_exec(name: str, args: dict) -> str:
                    if name == "submit_result":
                        return json.dumps({"status": "received"})
                    return await coord.mcp_executor.execute_tool(
                        name, args, planner_tools,
                    )

                _plan_token_cb = coord._build_token_streamer()
                content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_with_submit,
                    tool_executor=_plan_tool_exec,
                    max_rounds=70,
                    on_activity=lambda msg: coord._report_planning_activity(msg),
                    on_cancel_check=coord._is_cancelled,
                    on_token=_plan_token_cb,
                    temperature=0.1,
                    max_tokens=16384,
                )
                if hasattr(_plan_token_cb, "flush"):
                    await _plan_token_cb.flush()
            else:
                response = await provider.send_message(
                    messages, temperature=0.1, max_tokens=16384,
                    tools=[plan_submit_tool],
                    tool_choice={"name": "submit_result"},
                )
                content = response.content
            await coord._track_tokens(response)

            plan = extract_submit_result(
                response.tool_calls, content, messages=messages,
            )
            if plan is None:
                plan = parse_llm_json(content)
            if plan is not None:
                logger.info(
                    "[%s] Plan parsed on attempt %d: keys=%s, sub_tasks=%d",
                    coord.todo_id, attempt + 1, list(plan.keys()),
                    len(plan.get("sub_tasks", [])),
                )
                if plan.get("sub_tasks"):
                    for i, st in enumerate(plan["sub_tasks"]):
                        logger.info(
                            "[%s]   sub_task[%d]: role=%s title=%s target_repo=%s deps=%s review_loop=%s",
                            coord.todo_id, i, st.get("agent_role"), st.get("title"),
                            st.get("target_repo", "main"), st.get("depends_on", []),
                            st.get("review_loop", False),
                        )
                    if dep_names and all(
                        st.get("target_repo", "main").lower() == "main"
                        for st in plan["sub_tasks"]
                    ):
                        logger.warning(
                            "[%s] Plan has ALL sub-tasks targeting 'main' but project has deps: %s. "
                            "Planner may have missed cross-repo routing.",
                            coord.todo_id, dep_names,
                        )
                else:
                    logger.warning("[%s] Plan has ZERO sub_tasks! Raw content: %.500s",
                                   coord.todo_id, content)
                break

            content_len = len(content or "")
            logger.warning(
                "[%s] Plan parse attempt %d/%d failed. content_length=%d, "
                "stop_reason=%s, model=%s\nFull LLM response:\n%s",
                coord.todo_id, attempt + 1, max_retries, content_len,
                response.stop_reason, response.model,
                content or "(empty)",
            )

            if attempt < max_retries - 1:
                planner_tools = None
                messages = [
                    LLMMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
                    LLMMessage(
                        role="user",
                        content=(
                            f"Task: {todo['title']}\n"
                            f"Description: {todo['description'] or 'N/A'}\n"
                            f"Type: {todo['task_type']}\n"
                            f"Intake data: {json.dumps(intake_data, default=str)}\n"
                            f"Project context: {json.dumps(context, default=str)}\n\n"
                            "IMPORTANT: Your previous response was not valid JSON and could not be parsed. "
                            "You MUST respond with ONLY a valid JSON object. No markdown fences, no explanation, "
                            "no text before or after the JSON. Just the raw JSON object starting with {{ and ending with }}."
                        ),
                    ),
                ]
            else:
                truncated = (content or "")[-2000:]
                if content_len > 2000:
                    truncated = f"…(truncated, full length={content_len})\n{truncated}"
                await coord._post_system_message(
                    f"**Planning failed** — could not parse plan after {max_retries} attempts.\n\n"
                    f"**Stop reason:** `{response.stop_reason}` | **Model:** `{response.model}` | "
                    f"**Response length:** {content_len} chars\n\n"
                    f"<details><summary>Raw LLM response (last 2000 chars)</summary>\n\n"
                    f"```\n{truncated}\n```\n</details>"
                )
                raise ValueError("Failed to parse execution plan from LLM after retries")

        # ── Plan review loop: LLM validates quality before proceeding ──
        task_context = (
            f"Task: {todo['title']}\n"
            f"Description: {todo['description'] or 'N/A'}\n"
            f"Type: {todo['task_type']}"
        )
        max_review_iterations = 2
        for review_iter in range(max_review_iterations + 1):
            review = await self.review_plan(plan, task_context, provider, context=context)

            if review["approved"]:
                logger.info("[%s] Plan review approved (iteration %d)", coord.todo_id, review_iter)
                break

            if review_iter == max_review_iterations:
                logger.warning(
                    "[%s] Plan review: max iterations (%d) reached, proceeding with current plan",
                    coord.todo_id, max_review_iterations,
                )
                break

            feedback = review.get("feedback", "Plan needs improvement.")
            await coord._post_system_message(
                f"**Plan review (iteration {review_iter + 1}):** Revising plan...\n\n{feedback}"
            )
            plan = await self.re_plan_with_feedback(
                plan, feedback, provider, todo, context,
                workspace_path=workspace_path,
                planner_tools=planner_tools,
            )

            await coord.db.execute(
                "UPDATE todo_items SET plan_json = $2, updated_at = NOW() WHERE id = $1",
                coord.todo_id, plan,
            )

        # Store final plan
        await coord.db.execute(
            "UPDATE todo_items SET plan_json = $2, updated_at = NOW() WHERE id = $1",
            coord.todo_id,
            plan,
        )

        sub_tasks_text = "\n".join(
            f"  {i+1}. [{st['agent_role']}] {st['title']}"
            for i, st in enumerate(plan.get("sub_tasks", []))
        )

        # Check if human approval is required
        project_row = await coord.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1",
            todo["project_id"],
        )
        project_settings = project_row["settings_json"] or {} if project_row else {}
        if isinstance(project_settings, str):
            project_settings = json.loads(project_settings)
        require_plan_approval = project_settings.get("require_plan_approval", False)
        has_chat_session = bool(getattr(coord, "_chat_session_id", None))

        if require_plan_approval or has_chat_session:
            await coord._transition_todo("plan_ready")

            plan_message = (
                f"**Plan ready for review** ({len(plan.get('sub_tasks', []))} sub-tasks)\n\n"
                f"{plan.get('summary', 'No summary')}\n\n"
                f"**Sub-tasks:**\n{sub_tasks_text}"
            )

            if has_chat_session:
                plan_metadata = {
                    "action": "task_plan_ready",
                    "task_id": coord.todo_id,
                    "task_title": todo.get("title", ""),
                    "plan_data": {
                        "summary": plan.get("summary", ""),
                        "sub_tasks": [
                            {
                                "title": st.get("title", ""),
                                "agent_role": st.get("agent_role", ""),
                                "description": st.get("description", ""),
                                "execution_order": st.get("execution_order", 0),
                                "depends_on": st.get("depends_on", []),
                                "review_loop": st.get("review_loop", False),
                                "target_repo": st.get("target_repo", "main"),
                                "context": st.get("context", {}),
                            }
                            for st in plan.get("sub_tasks", [])
                        ],
                    },
                }
                await coord._post_system_message(
                    plan_message + "\n\nApprove or reject below.",
                    metadata=plan_metadata,
                )
            else:
                await coord._post_system_message(
                    plan_message + "\n\nApprove or reject from the task detail page.",
                )
        else:
            # Auto-approve (default behavior)
            await coord._post_system_message(
                f"**Execution Plan:**\n\n{plan.get('summary', 'No summary')}\n\n"
                f"**Sub-tasks:** {len(plan.get('sub_tasks', []))}\n"
                + sub_tasks_text
                + "\n\nAuto-approved. Starting execution."
            )
            await self.auto_approve_plan(todo, plan)

    # ------------------------------------------------------------------
    # Plan review
    # ------------------------------------------------------------------

    async def review_plan(
        self, plan: dict, task_context: str, provider: AIProvider, *, context: dict | None = None,
    ) -> dict:
        """LLM-based plan quality review. Returns {"approved": bool, "feedback": str}."""
        coord = self._coord
        from agents.orchestrator.structured_output import build_submit_tool, extract_submit_result

        dep_names_for_review = list(context.get("dependency_dirs", {}).keys()) if context else []
        review_system = (
            "You are a plan reviewer for an AI task orchestration system. "
            "Your job is to evaluate whether the execution plan is high quality and ready to execute.\n\n"
            "Review criteria:\n"
            "1. Every coder/debugger sub-task has specific context — references actual file paths, not vague descriptions\n"
            "2. Sub-task descriptions are actionable and clear about what needs to change\n"
            "3. Dependencies are logical (no circular deps, no unnecessary sequential chains)\n"
            "4. The plan covers the full scope of the original task\n"
            "5. Agent roles are appropriate for the work described\n"
            "6. Parallelism is maximized — flag plans that are unnecessarily sequential\n"
            "7. Sub-tasks have populated context objects with relevant_files where applicable\n"
            "8. target_repo is correctly set — sub-tasks modifying a dependency repo MUST set "
            "target_repo to the dependency name, NOT 'main'. Sub-tasks for the main repo use 'main'.\n\n"
        )
        if dep_names_for_review:
            review_system += (
                f"Available dependency repos: {', '.join(dep_names_for_review)}. "
                "If the task involves changes to any of these repos, verify that the relevant "
                "sub-tasks have target_repo set to the dependency name.\n\n"
            )
        review_system += (
            "If the plan is good enough to execute, approve it. Only reject if there are clear, "
            "fixable issues. Be practical — don't reject for minor style preferences."
        )

        review_schema = {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean", "description": "Whether the plan passes review"},
                "feedback": {"type": "string", "description": "Specific feedback if rejecting, or brief approval note"},
            },
            "required": ["approved", "feedback"],
        }
        review_submit_tool = build_submit_tool(review_schema, "review",
            "Submit your plan review verdict.")

        plan_text = json.dumps(plan, indent=2, default=str)
        if len(plan_text) > 8000:
            plan_text = plan_text[:8000] + "\n...(truncated)"

        messages = [
            LLMMessage(role="system", content=review_system),
            LLMMessage(
                role="user",
                content=(
                    f"{task_context}\n\n"
                    f"## Execution Plan to Review\n```json\n{plan_text}\n```\n\n"
                    "Review this plan against the criteria and submit your verdict."
                ),
            ),
        ]

        try:
            response = await provider.send_message(
                messages, temperature=0.1, max_tokens=2048,
                tools=[review_submit_tool],
                tool_choice={"name": "submit_result"},
            )
            await coord._track_tokens(response)

            result = extract_submit_result(response.tool_calls, response.content)
            if result and isinstance(result.get("approved"), bool):
                logger.info(
                    "[%s] Plan review verdict: approved=%s feedback=%s",
                    coord.todo_id, result["approved"], result.get("feedback", "")[:200],
                )
                return result
        except Exception:
            logger.warning("[%s] Plan review call failed, auto-approving", coord.todo_id, exc_info=True)

        return {"approved": True, "feedback": "Review skipped (error)"}

    # ------------------------------------------------------------------
    # Re-plan with feedback
    # ------------------------------------------------------------------

    async def re_plan_with_feedback(
        self,
        previous_plan: dict,
        feedback: str,
        provider: AIProvider,
        todo: dict,
        context: dict,
        *,
        workspace_path: str | None = None,
        planner_tools: list | None = None,
    ) -> dict:
        """Re-run the planner with review feedback injected. Returns updated plan."""
        coord = self._coord
        from agents.orchestrator.structured_output import build_submit_tool, extract_submit_result

        replan_dep_names = list(context.get("dependency_dirs", {}).keys()) if context else []
        if replan_dep_names:
            allowed_values = ", ".join(f"'{n}'" for n in replan_dep_names)
            replan_target_desc = (
                f"REQUIRED. Use 'main' for main-repo work. "
                f"Available dependency repos: {allowed_values}. "
                f"Set to the EXACT dependency name for sub-tasks that modify a dependency repo."
            )
        else:
            replan_target_desc = "REQUIRED. 'main' for main-repo work, or the dependency name for dep-repo work."

        plan_schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief plan summary"},
                "sub_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "agent_role": {"type": "string"},
                            "execution_order": {"type": "integer"},
                            "depends_on": {"type": "array", "items": {"type": "integer"}},
                            "review_loop": {"type": "boolean"},
                            "target_repo": {
                                "type": "string",
                                "description": replan_target_desc,
                            },
                        },
                        "required": ["title", "description", "agent_role", "target_repo"],
                    },
                },
                "estimated_tokens": {"type": "integer"},
            },
            "required": ["summary", "sub_tasks"],
        }
        plan_submit_tool = build_submit_tool(plan_schema, "plan",
            "Submit your revised execution plan.")

        prev_plan_text = json.dumps(previous_plan, indent=2, default=str)
        if len(prev_plan_text) > 6000:
            prev_plan_text = prev_plan_text[:6000] + "\n...(truncated)"

        replan_user_content = (
            f"Task: {todo['title']}\n"
            f"Description: {todo['description'] or 'N/A'}\n"
            f"Type: {todo['task_type']}\n"
        )
        if replan_dep_names:
            dep_str = ", ".join(f"'{n}'" for n in replan_dep_names)
            replan_user_content += (
                f"\nAvailable dependency repos: {dep_str}. "
                "Set target_repo to the dependency name for sub-tasks modifying a dep repo.\n"
            )
        replan_user_content += (
            f"\n## Previous Plan\n```json\n{prev_plan_text}\n```\n\n"
            f"## Review Feedback\n{feedback}\n\n"
            "Revise the plan to address the review feedback. "
            "You already explored the codebase — focus on fixing the identified issues. "
            "Submit the complete revised plan via submit_result."
        )

        messages = [
            LLMMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
            LLMMessage(role="user", content=replan_user_content),
        ]

        tools_for_replan = [plan_submit_tool]
        if planner_tools:
            tools_for_replan = list(planner_tools) + [plan_submit_tool]

        try:
            if planner_tools:
                from agents.providers.base import run_tool_loop

                async def _replan_tool_exec(name: str, args: dict) -> str:
                    if name == "submit_result":
                        return json.dumps({"status": "received"})
                    return await coord.mcp_executor.execute_tool(
                        name, args, planner_tools,
                    )

                _replan_token_cb = coord._build_token_streamer()
                content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_for_replan,
                    tool_executor=_replan_tool_exec,
                    max_rounds=30,
                    on_activity=lambda msg: coord._report_planning_activity(msg),
                    on_cancel_check=coord._is_cancelled,
                    on_token=_replan_token_cb,
                    temperature=0.1,
                    max_tokens=16384,
                )
                if hasattr(_replan_token_cb, "flush"):
                    await _replan_token_cb.flush()
            else:
                response = await provider.send_message(
                    messages, temperature=0.1, max_tokens=16384,
                    tools=[plan_submit_tool],
                    tool_choice={"name": "submit_result"},
                )
                content = response.content

            await coord._track_tokens(response)

            revised = extract_submit_result(response.tool_calls, content, messages=messages)
            if revised is None:
                revised = parse_llm_json(content)

            if revised and revised.get("sub_tasks"):
                logger.info(
                    "[%s] Re-plan produced %d sub-tasks",
                    coord.todo_id, len(revised["sub_tasks"]),
                )
                return revised
        except Exception:
            logger.warning("[%s] Re-plan failed, keeping previous plan", coord.todo_id, exc_info=True)

        return previous_plan

    # ------------------------------------------------------------------
    # Auto-approve plan
    # ------------------------------------------------------------------

    async def auto_approve_plan(self, todo: dict, plan: dict) -> None:
        """Create sub-tasks from plan and transition directly to execution."""
        coord = self._coord
        from agents.utils.repo_utils import resolve_target_repo

        logger.info("[%s] auto_approve_plan: plan keys=%s, sub_tasks=%d",
                    coord.todo_id, list(plan.keys()), len(plan.get("sub_tasks", [])))
        if not plan.get("sub_tasks"):
            logger.error("[%s] auto_approve_plan: NO sub_tasks in plan! Plan: %.1000s",
                         coord.todo_id, json.dumps(plan, default=str))

        # Load context_docs for deterministic target_repo resolution
        project = await coord.db.fetchrow(
            "SELECT context_docs FROM projects WHERE id = $1", todo["project_id"],
        )
        context_docs = []
        if project and project.get("context_docs"):
            context_docs = project["context_docs"]
            if isinstance(context_docs, str):
                context_docs = json.loads(context_docs)

        sub_task_ids = []
        plan_index_to_id: dict[int, str] = {}
        for i, st in enumerate(plan.get("sub_tasks", [])):
            raw_target = st.get("target_repo")
            target_repo = resolve_target_repo(raw_target, context_docs)
            if target_repo:
                logger.info("[%s] Resolved target_repo '%s' → %s", coord.todo_id, raw_target, target_repo.get("name"))
            elif raw_target and str(raw_target).strip().lower() != "main":
                logger.warning("[%s] Could not resolve target_repo '%s', falling back to main repo", coord.todo_id, raw_target)
            logger.info("[%s] Inserting sub_task[%d]: role=%s title=%s order=%s deps=%s review_loop=%s",
                        coord.todo_id, i, st.get("agent_role"), st.get("title"),
                        st.get("execution_order", 0), st.get("depends_on", []),
                        st.get("review_loop", False))
            try:
                row = await coord.db.fetchrow(
                    """
                    INSERT INTO sub_tasks (
                        todo_id, title, description, agent_role,
                        execution_order, input_context,
                        review_loop, target_repo
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    coord.todo_id,
                    st["title"],
                    st.get("description", ""),
                    st["agent_role"],
                    st.get("execution_order", 0),
                    st.get("context", {}),
                    bool(st.get("review_loop", False)),
                    target_repo,
                )
                st_id = str(row["id"])
                sub_task_ids.append(st_id)
                plan_index_to_id[i] = st_id
                logger.info("[%s] Inserted sub_task[%d] id=%s", coord.todo_id, i, st_id)

                if st.get("review_loop"):
                    await coord.db.execute(
                        "UPDATE sub_tasks SET review_chain_id = $1 WHERE id = $1",
                        row["id"],
                    )
            except Exception:
                logger.exception("[%s] FAILED to insert sub_task[%d]: %s",
                                 coord.todo_id, i, st.get("title"))

        # Set up dependencies using plan index mapping
        for i, st in enumerate(plan.get("sub_tasks", [])):
            if i not in plan_index_to_id:
                continue
            depends_on = st.get("depends_on", [])
            if depends_on:
                dep_ids = [plan_index_to_id[j] for j in depends_on if j in plan_index_to_id]
                if dep_ids:
                    await coord.db.execute(
                        "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
                        plan_index_to_id[i],
                        dep_ids,
                    )

        logger.info("[%s] Created %d sub-tasks (ids=%s), transitioning to in_progress/executing",
                    coord.todo_id, len(sub_task_ids), sub_task_ids)
        result = await coord._transition_todo("in_progress", sub_state="executing")
        if result:
            logger.info("[%s] Transitioned to in_progress successfully (state=%s, sub_state=%s)",
                        coord.todo_id, result.get("state"), result.get("sub_state"))
        else:
            logger.error("[%s] FAILED to transition to in_progress! Optimistic lock failed or invalid state",
                         coord.todo_id)

        # Notify human that execution has started
        await coord.notifier.notify(
            str(todo["creator_id"]),
            "in_progress",
            {
                "todo_id": coord.todo_id,
                "title": todo["title"],
                "detail": f"Auto-approved plan with {len(sub_task_ids)} sub-tasks. Executing.",
            },
        )

        # Immediately start execution
        logger.info("[%s] Resolving provider for execution...", coord.todo_id)
        todo = await coord._load_todo()
        provider = await coord.provider_registry.resolve_for_todo(coord.todo_id)
        logger.info("[%s] Provider resolved: %s/%s. Starting execution",
                    coord.todo_id, provider.provider_type, provider.default_model)
        await coord._execution.run(todo, provider)
