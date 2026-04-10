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
from agents.orchestrator.workspace import MAIN_REPO
from agents.schemas.agent import LLMMessage
from agents.utils.json_helpers import parse_llm_json, safe_json

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """\
You are a principal software architect. Your job is to deeply understand a codebase, \
reason about the right approach, and produce a precise execution plan.

## Your Mindset
Think like an architect who owns the system. Before touching anything:
- Understand the EXISTING architecture, patterns, and conventions
- Identify WHERE in the system the change belongs
- Determine the MINIMAL set of changes needed
- Verify your understanding by reading actual code — never assume

## Workflow

### Phase 1: Understand
You are given Project Knowledge below (architecture, patterns, deps, build workflow). \
READ IT FIRST — it already answers most architectural questions. Only use tools for \
targeted verification:
1. Use search_files to find the specific files related to this task
2. Read 2-3 key files to understand the current code that needs to change
3. Do NOT broadly explore — the project knowledge section has the architecture

CRITICAL: Every file path you reference in your plan MUST be one you actually read or \
found via tools. NEVER guess or hallucinate file paths. If you're unsure where something \
lives, search for it first.

### Phase 2: Reason about approach
Before writing the plan, think through:
- What's the right way to implement this given the existing patterns?
- What files need to change? What new files need to be created? Where?
- Are there existing examples of similar features I can point agents to?
- What could go wrong? What are the edge cases?
- What's the right order of operations?

### Phase 3: Plan
Output a precise execution plan as JSON.

## Planning Rules

PARALLELISM — you have multiple agents that run concurrently:
- Split work so agents touch DIFFERENT files — no conflicts
- Sub-tasks at the same execution_order with no depends_on run in parallel
- Use depends_on ONLY for true data dependencies (task B needs a file task A creates)
- Split by file, by layer (frontend/backend), by feature, or by repo
- If a shared type is needed by multiple tasks, create it first (order=0), \
then dependents run in parallel (order=1)

AGENT ROLES:
- **coder** — All code changes. Give it specific files, patterns, and exact instructions.
- **debugger** — Bug investigation using logs, metrics, DB queries. For incidents/errors.
- **tester** — Writes and runs tests to validate changes.
- **reviewer** — Code review for quality, security, correctness.
- **report_writer** — Documentation and reports.

Do NOT create pr_creator or merge_agent sub-tasks — the system handles PR/merge automatically.

REVIEW LOOP: Set review_loop=true for critical changes (core logic, security, API contracts, \
DB migrations). The system chains coder→reviewer→PR→merge automatically.

## Sub-Task Quality Standards

Each sub-task description must be a complete brief that an agent can execute without \
re-exploring the codebase. Include:

1. **VERIFIED file paths** — only paths you confirmed exist (or explicit "create new file at X")
2. **Current state** — what the code does now (paste key snippets you read)
3. **Exact changes** — not "update the API" but "add field X to the response type at line Y \
in file Z, following the pattern used by field W"
4. **Patterns to follow** — point to a specific existing example: "follow the pattern in \
src/routes/users.py which does the same thing for user profiles"
5. **Integration context** — how this change connects to other sub-tasks or systems

BAD sub-task: "Add the new field to the API response"
GOOD sub-task: "Add isLLMChatEnabled boolean to ProfileRes type in Backend/spec/profile.yaml \
(line 45, alongside existing fields like hasCompletedSafetySetup). Then update \
Backend/src/Profile/Handler.hs makeProfileRes function to compute the value from RiderConfig. \
Follow the exact pattern used by hasCompletedSafetySetup — see line 89 of Handler.hs."

## Cross-Repo Work

Dependency repos are at ../deps/{name}/ (read-only). Explore them with:
- list_directory(path="../deps/") — see available repos
- read_file(path="../deps/{name}/src/...") — read dependency code
- search_files(pattern="...", path="../deps/{name}/") — exact string search within a dep

For sub-tasks that MODIFY a dependency, set target_repo to the dependency name. \
The orchestrator creates a writeable workspace automatically.

When the task spans repos, explore BOTH repos and document the integration points \
(API contracts, shared types, data flow) in each sub-task's context.

## Output Format

After exploration, call submit_result with your plan. The plan MUST have this structure:

```json
{
  "summary": "Brief description of the overall plan",
  "sub_tasks": [
    {
      "title": "Short descriptive title",
      "description": "Detailed instructions with verified file paths and exact changes",
      "agent_role": "coder",
      "execution_order": 0,
      "depends_on": [],
      "review_loop": false,
      "target_repo": "main",
      "context": {
        "relevant_files": ["path/to/file.py"],
        "current_state": "What the code currently does",
        "what_to_change": "Specific changes needed",
        "patterns_to_follow": "Example from codebase to follow",
        "related_code": "Key function signatures",
        "integration_points": "How this connects to other parts"
      }
    }
  ],
  "estimated_tokens": 5000
}
```

Rules:
- sub_tasks array MUST have at least 1 entry — a plan with 0 sub-tasks is invalid
- agent_role: coder | debugger | tester | reviewer | report_writer
- target_repo: "main" or the exact dependency name
- context is REQUIRED for coder and debugger roles
- execution_order 0 = first wave (parallel), 1+ = sequential after dependencies
- depends_on: 0-based indexes of sub-tasks that must complete first
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

        # Load project settings early — used for guidelines injection and plan approval check
        from agents.utils.settings_helpers import parse_settings, read_setting
        _proj_settings_row = await coord.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", todo["project_id"],
        )
        _proj_settings = parse_settings((_proj_settings_row or {}).get("settings_json"))

        # Inject planning guidelines if configured
        _guidelines = read_setting(_proj_settings, "planning.guidelines", "planning_guidelines", "")
        if _guidelines:
            planner_prompt += f"\n\n## Project Planning Guidelines\n{_guidelines}\n"

        # Resolve workspace for codebase exploration
        # setup_task_workspace returns task_root (tasks/{todo_id}/)
        # The main repo workspace is at task_root/repos/main/
        workspace_path = None  # git working dir for main repo
        task_root = None
        try:
            project = await coord.db.fetchrow(
                "SELECT repo_url, workspace_path FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if project and project.get("repo_url"):
                task_root = await coord.workspace_mgr.setup_task_workspace(coord.todo_id)
                workspace_path = os.path.join(task_root, MAIN_REPO)
                logger.info("[%s] Planner workspace ready at %s", coord.todo_id, workspace_path)
        except Exception:
            logger.exception("[%s] Could not set up planner workspace — planning will proceed without workspace tools",
                             coord.todo_id)

        # ── Inject project knowledge so the planner doesn't re-explore everything ──
        # This is the most important section — it gives the planner the analysis
        # results so it can plan with knowledge instead of spending 100K tokens
        # rediscovering what the project already analyzed.

        understanding = context.get("project_understanding", {})
        if understanding and isinstance(understanding, dict):
            summary = understanding.get("summary", "")
            arch = understanding.get("architecture", "")
            tech = understanding.get("tech_stack", [])
            build_workflow = understanding.get("build_workflow", "")
            key_patterns = understanding.get("key_patterns", [])
            api_surface = understanding.get("api_surface", "")

            planner_prompt += "\n\n## Project Knowledge (from analysis — use this, don't re-explore)"
            if summary:
                planner_prompt += f"\n{summary}"
            if tech:
                planner_prompt += f"\nTech stack: {', '.join(tech[:10])}"
            if arch:
                planner_prompt += f"\n\nArchitecture:\n{arch[:1000]}"
            if build_workflow:
                planner_prompt += f"\n\nBuild workflow:\n{build_workflow[:500]}"
            if key_patterns:
                planner_prompt += "\n\nKey patterns:\n" + "\n".join(f"- {p}" for p in key_patterns[:8])
            if api_surface:
                planner_prompt += f"\n\nAPI surface:\n{api_surface[:500]}"

        dep_understandings = context.get("dep_understandings", {})
        if dep_understandings and isinstance(dep_understandings, dict):
            planner_prompt += "\n\n## Dependency Repos"
            for dep_name, dep_u in dep_understandings.items():
                if isinstance(dep_u, dict):
                    purpose = dep_u.get("purpose", "")
                    api = dep_u.get("api_surface", "")
                    dep_arch = dep_u.get("architecture", "")
                    planner_prompt += f"\n\n### {dep_name}"
                    if purpose:
                        planner_prompt += f"\n{purpose}"
                    if dep_arch:
                        planner_prompt += f"\nArchitecture: {dep_arch[:300]}"
                    if api:
                        planner_prompt += f"\nAPI: {api[:400]}"

        linking_doc = context.get("linking_document", {})
        if linking_doc and isinstance(linking_doc, dict):
            overview = linking_doc.get("overview", "")
            if overview:
                planner_prompt += f"\n\n## Cross-Repo Integration\n{overview[:800]}"
            integrations = linking_doc.get("integrations", [])
            if integrations:
                for intg in integrations[:6]:
                    src = intg.get("source_repo", "")
                    tgt = intg.get("target_repo", "")
                    pattern = intg.get("pattern", "")
                    if src and tgt:
                        planner_prompt += f"\n- {src} → {tgt}: {pattern[:150]}"

        # ── Workspace tools and file tree ──
        if workspace_path:
            file_tree = coord.workspace_mgr.get_file_tree(workspace_path, max_depth=3)
            planner_prompt += f"\n\n## File Structure\n{file_tree}\n"

            planner_prompt += (
                "\nUse tools to verify specific files ONLY when the project knowledge above "
                "doesn't have what you need. Don't re-explore what's already documented above."
            )

            dep_dirs = context.get("dependency_dirs", {})
            deps_list = context.get("dependencies", [])
            if dep_dirs:
                planner_prompt += "\n\nDependency repos (read-only at ../deps/{name}/, writable via target_repo):"
                for dep in deps_list:
                    name = dep.get("name", "")
                    dir_name = dep_dirs.get(name, "")
                    if dir_name:
                        planner_prompt += f"\n  - \"{name}\" (browse at ../deps/{dir_name}/)"

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
                "Sub-tasks modifying a dep MUST set target_repo to the dep name."
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
        _captured_plan_data: dict | None = None
        max_retries = 3
        for attempt in range(max_retries):
            if planner_tools:
                from agents.providers.base import run_tool_loop

                tools_with_submit = list(planner_tools) + [plan_submit_tool]

                async def _plan_tool_exec(name: str, args: dict) -> str:
                    nonlocal _captured_plan_data
                    if name == "submit_result":
                        _captured_plan_data = args
                        return json.dumps({"status": "received"})
                    return await coord.mcp_executor.execute_tool(
                        name, args, planner_tools,
                    )

                async def _plan_tool_event(event: dict) -> None:
                    """Publish planning tool events for frontend visibility."""
                    try:
                        event["phase"] = "planning"
                        event["ts"] = time.time()
                        payload = json.dumps(event)
                        await coord.redis.publish(
                            f"task:{coord.todo_id}:events", payload,
                        )
                        if getattr(coord, "_chat_session_id", None):
                            await coord.redis.publish(
                                f"chat:session:{coord._chat_session_id}:activity",
                                payload,
                            )
                    except Exception:
                        pass

                _plan_token_cb = coord._build_token_streamer()
                content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_with_submit,
                    tool_executor=_plan_tool_exec,
                    max_rounds=500,
                    on_activity=lambda msg: coord._report_planning_activity(msg),
                    on_tool_event=_plan_tool_event,
                    on_cancel_check=coord._is_cancelled,
                    on_token=_plan_token_cb,
                    temperature=0.1,
                    max_tokens=16384,
                )
                if hasattr(_plan_token_cb, "flush"):
                    await _plan_token_cb.flush()
            else:
                if attempt == 0:
                    # First attempt without workspace — use structured submit tool
                    response = await provider.send_message(
                        messages, temperature=0.1, max_tokens=16384,
                        tools=[plan_submit_tool],
                        tool_choice={"name": "submit_result"},
                    )
                else:
                    # Retry — ask for raw JSON text without tools.  Models that
                    # don't support tool_choice (e.g. kimi) return empty args
                    # when forced, so plain-text JSON is more reliable.
                    response = await provider.send_message(
                        messages, temperature=0.1, max_tokens=16384,
                    )
                content = response.content
            await coord._track_tokens(response)

            # Use captured plan from tool executor (most reliable)
            plan = _captured_plan_data
            # If captured data has _raw_arguments (unparsed JSON string from provider),
            # parse it — this happens with models that don't produce valid JSON tool args
            if plan and "_raw_arguments" in plan and "sub_tasks" not in plan:
                raw = plan.get("_raw_arguments", "")
                if raw:
                    parsed = parse_llm_json(raw)
                    if parsed and parsed.get("sub_tasks"):
                        plan = parsed
                        logger.info("[%s] Parsed plan from captured _raw_arguments", coord.todo_id)
            if plan is None:
                plan = extract_submit_result(response.tool_calls, content)
            if plan is None:
                plan = parse_llm_json(content)
            # Last resort: raw string args from tool_calls
            if plan is None and response.tool_calls:
                for tc in response.tool_calls:
                    raw = tc.get("arguments", {}).get("_raw_arguments", "")
                    if raw:
                        plan = parse_llm_json(raw)
                        if plan:
                            logger.info("[%s] Recovered plan from _raw_arguments fallback", coord.todo_id)
                            break
            _captured_plan_data = None  # Reset for next retry
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
                    logger.warning("[%s] Plan has ZERO sub_tasks on attempt %d, will retry",
                                   coord.todo_id, attempt + 1)
                    plan = None  # Force retry
                    continue
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
                # Retry with a minimal, focused system prompt — no tool references,
                # just clear instructions to output JSON.
                retry_system = (
                    "You are a project planner. Based on the task and context below, "
                    "produce an execution plan as a JSON object.\n\n"
                    "Output ONLY a raw JSON object (no markdown fences, no explanation):\n"
                    '{"summary":"...", "sub_tasks":[{"title":"...", "description":"...", '
                    '"agent_role":"coder|tester|reviewer|debugger|report_writer", '
                    '"execution_order":0, "depends_on":[], "review_loop":false, '
                    '"target_repo":"main", "context":{"relevant_files":[], '
                    '"what_to_change":"..."}}]}\n\n'
                    "Rules:\n"
                    "- Split work into parallel sub-tasks (same execution_order, no depends_on)\n"
                    "- Be specific in descriptions — include file paths and what to change\n"
                    "- Every sub-task MUST have target_repo set (use 'main' for main repo)"
                )
                messages = [
                    LLMMessage(role="system", content=retry_system),
                    LLMMessage(
                        role="user",
                        content=(
                            f"Task: {todo['title']}\n"
                            f"Description: {todo['description'] or 'N/A'}\n"
                            f"Type: {todo['task_type']}\n"
                            f"Intake data: {json.dumps(intake_data, default=str)}\n"
                            f"Project context: {json.dumps(context, default=str)}\n\n"
                            "Respond with ONLY the JSON plan object."
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
            review = await self.review_plan(
                plan, task_context, provider,
                context=context, workspace_path=workspace_path,
            )

            review_metadata = {
                "action": "plan_review_verdict",
                "task_id": coord.todo_id,
                "approved": review["approved"],
                "feedback": review.get("feedback", ""),
                "iteration": review_iter + 1,
            }

            if review["approved"]:
                logger.info("[%s] Plan review approved (iteration %d)", coord.todo_id, review_iter)
                await coord._post_system_message(
                    f"**Plan review:** Approved",
                    metadata=review_metadata,
                )
                break

            if review_iter == max_review_iterations:
                logger.warning(
                    "[%s] Plan review: max iterations (%d) reached, proceeding with current plan",
                    coord.todo_id, max_review_iterations,
                )
                await coord._post_system_message(
                    f"**Plan review:** Max revisions reached, proceeding with current plan.",
                    metadata=review_metadata,
                )
                break

            feedback = review.get("feedback", "Plan needs improvement.")
            await coord._post_system_message(
                f"**Plan review (iteration {review_iter + 1}):** Revising plan...\n\n{feedback}",
                metadata=review_metadata,
            )
            previous_plan = plan
            plan = await self.re_plan_with_feedback(
                plan, feedback, provider, todo, context,
                workspace_path=workspace_path,
                planner_tools=planner_tools,
                planner_system_prompt=planner_prompt,
            )

            # Detect when re-plan failed and returned the same plan unchanged
            prev_titles = {st.get("title") for st in previous_plan.get("sub_tasks", [])}
            new_titles = {st.get("title") for st in plan.get("sub_tasks", [])}
            if prev_titles == new_titles and len(prev_titles) == len(plan.get("sub_tasks", [])):
                logger.warning(
                    "[%s] Re-plan returned identical plan (re-plan extraction likely failed), stopping review loop",
                    coord.todo_id,
                )
                await coord._post_system_message(
                    f"**Plan review:** Re-plan could not produce a revised plan. Proceeding with current plan.",
                    metadata={**review_metadata, "replan_failed": True},
                )
                break

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

        # Check if human approval is required (using settings loaded at start of run())
        require_plan_approval = read_setting(
            _proj_settings, "planning.require_approval", "require_plan_approval", False,
        )
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

    @staticmethod
    def _verify_plan_paths(plan: dict, workspace_path: str | None) -> str:
        """Check which file paths in the plan actually exist on disk.

        Returns a summary string for the reviewer, e.g.:
          VERIFIED (exists): Backend/spec/profile.yaml, Backend/src/Handler.hs
          NOT FOUND: consumer/src/chat/ChatScreen.tsx
          NEW FILE (to be created): Backend/spec/chat.yaml
        """
        if not workspace_path:
            return ""

        all_paths: dict[str, str] = {}  # path → subtask title
        for st in plan.get("sub_tasks", []):
            ctx = st.get("context", {})
            if not isinstance(ctx, dict):
                continue
            title = st.get("title", "?")
            for f in ctx.get("relevant_files", []):
                if isinstance(f, str) and f.strip():
                    all_paths[f.strip()] = title

        if not all_paths:
            return ""

        verified = []
        not_found = []
        for path, title in sorted(all_paths.items()):
            full = os.path.normpath(os.path.join(workspace_path, path))
            if os.path.exists(full):
                verified.append(path)
            else:
                not_found.append(path)

        lines = []
        if verified:
            lines.append(f"VERIFIED (exist on disk): {', '.join(verified[:20])}")
        if not_found:
            lines.append(f"NOT FOUND on disk: {', '.join(not_found[:20])}")
            lines.append(
                "Note: 'not found' paths may be files the agent will CREATE — "
                "only flag as hallucinated if the plan says to MODIFY an existing file "
                "but the path doesn't exist."
            )
        return "\n".join(lines)

    async def review_plan(
        self, plan: dict, task_context: str, provider: AIProvider,
        *, context: dict | None = None, workspace_path: str | None = None,
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
            "4. The plan covers the full scope of the original task — nothing missing\n"
            "5. Agent roles are appropriate for the work described\n"
            "6. Parallelism is maximized — flag plans that are unnecessarily sequential\n"
            "7. File paths are verified — check the 'Path Verification' section in the review input. "
            "If paths meant to modify EXISTING files show as NOT FOUND, that's a real problem. "
            "Paths for NEW files to be created are fine even if not found.\n"
            "8. Sub-task descriptions are precise enough for an agent to execute WITHOUT re-exploring "
            "the codebase — they should include exact file paths, current code state, and specific changes\n"
            "9. target_repo is correctly set — sub-tasks modifying a dependency repo MUST set "
            "target_repo to the dependency name, NOT 'main'. Sub-tasks for the main repo use 'main'.\n"
            "10. The approach is architecturally sound — changes are in the right place, follow "
            "existing patterns, and don't introduce unnecessary complexity\n\n"
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

        # Pre-verify file paths mentioned in the plan so the reviewer
        # has facts instead of guessing whether paths are hallucinated.
        path_verification = self._verify_plan_paths(plan, workspace_path)

        review_user = f"{task_context}\n\n"
        if path_verification:
            review_user += f"## Path Verification (automated)\n{path_verification}\n\n"
        review_user += (
            f"## Execution Plan to Review\n```json\n{plan_text}\n```\n\n"
            "Review this plan against the criteria. The path verification above "
            "shows which files actually exist — use it to judge path accuracy. "
            "Submit your verdict."
        )

        messages = [
            LLMMessage(role="system", content=review_system),
            LLMMessage(role="user", content=review_user),
        ]

        try:
            response = await provider.send_message(
                messages, temperature=0.1, max_tokens=2048,
                tools=[review_submit_tool],
                tool_choice={"name": "submit_result"},
            )
            await coord._track_tokens(response)

            result = extract_submit_result(response.tool_calls, response.content)
            if result is None:
                result = parse_llm_json(response.content)
            if result and isinstance(result.get("approved"), bool):
                logger.info(
                    "[%s] Plan review verdict: approved=%s feedback=%s",
                    coord.todo_id, result["approved"], result.get("feedback", "")[:200],
                )
                return result

            # If tool-based extraction failed (e.g. model doesn't support
            # tool_choice), retry without tools asking for plain JSON.
            logger.warning("[%s] Plan review: tool extraction failed, retrying as plain JSON", coord.todo_id)
            messages.append(LLMMessage(role="assistant", content=response.content or ""))
            messages.append(LLMMessage(
                role="user",
                content=(
                    'Respond with ONLY a JSON object: {"approved": true/false, "feedback": "..."}\n'
                    "No other text."
                ),
            ))
            retry_resp = await provider.send_message(messages, temperature=0.1, max_tokens=1024)
            await coord._track_tokens(retry_resp)
            result = parse_llm_json(retry_resp.content)
            if result and isinstance(result.get("approved"), bool):
                logger.info(
                    "[%s] Plan review verdict (retry): approved=%s feedback=%s",
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
        planner_system_prompt: str | None = None,
    ) -> dict:
        """Re-run the planner with review feedback injected. Returns updated plan.

        planner_system_prompt: the full enriched prompt from the original planning
        run, including workspace context, dep info, and guidelines. If not provided,
        falls back to the bare PLANNER_SYSTEM_PROMPT (loses all codebase context).
        """
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
            f"\n## CRITICAL: Review Feedback (MUST address)\n{feedback}\n\n"
            f"## Previous Plan (rejected — see feedback above)\n```json\n{prev_plan_text}\n```\n\n"
            "You MUST revise the plan to address ALL points in the review feedback above. "
            "The previous plan was rejected specifically because of those issues. "
            "Do NOT return the same plan — the reviewer will reject it again. "
            "Use the workspace tools to explore the codebase if you need to understand "
            "what files exist in dependency repos before creating sub-tasks for them. "
            "Submit the complete revised plan via submit_result."
        )

        system_prompt = planner_system_prompt or PLANNER_SYSTEM_PROMPT
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=replan_user_content),
        ]

        tools_for_replan = [plan_submit_tool]
        if planner_tools:
            tools_for_replan = list(planner_tools) + [plan_submit_tool]

        _captured_plan: dict | None = None

        try:
            if planner_tools:
                from agents.providers.base import run_tool_loop

                async def _replan_tool_exec(name: str, args: dict) -> str:
                    nonlocal _captured_plan
                    if name == "submit_result":
                        _captured_plan = args  # Capture the plan data directly
                        return json.dumps({"status": "received"})
                    return await coord.mcp_executor.execute_tool(
                        name, args, planner_tools,
                    )

                _replan_token_cb = coord._build_token_streamer()
                content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_for_replan,
                    tool_executor=_replan_tool_exec,
                    max_rounds=500,
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

            # Use captured plan from tool executor (most reliable)
            revised = _captured_plan
            if revised and "_raw_arguments" in revised and "sub_tasks" not in revised:
                raw = revised.get("_raw_arguments", "")
                if raw:
                    parsed = parse_llm_json(raw)
                    if parsed and parsed.get("sub_tasks"):
                        revised = parsed
            if revised is None:
                revised = extract_submit_result(response.tool_calls, content)
            if revised is None:
                revised = parse_llm_json(content)

            if revised and revised.get("sub_tasks"):
                logger.info("[%s] Re-plan produced %d sub-tasks", coord.todo_id, len(revised["sub_tasks"]))
                return revised

            # Extraction failed — retry as plain JSON
            logger.warning(
                "[%s] Re-plan extraction failed (captured=%s, content_len=%d), retrying as plain JSON",
                coord.todo_id, _captured_plan is not None, len(content) if content else 0,
            )
            messages.append(LLMMessage(role="assistant", content=content or ""))
            messages.append(LLMMessage(role="user", content=(
                "Your response could not be parsed. Respond with ONLY a JSON object: "
                '{"summary": "...", "sub_tasks": [...]}. No markdown, no other text.'
            )))
            retry_resp = await provider.send_message(messages, temperature=0.1, max_tokens=16384)
            await coord._track_tokens(retry_resp)
            revised = parse_llm_json(retry_resp.content)
            if revised and revised.get("sub_tasks"):
                logger.info("[%s] Re-plan (retry) produced %d sub-tasks", coord.todo_id, len(revised["sub_tasks"]))
                return revised
        except Exception:
            logger.warning("[%s] Re-plan failed, keeping previous plan", coord.todo_id, exc_info=True)

        logger.warning(
            "[%s] Re-plan did not produce a valid revised plan, returning previous plan unchanged",
            coord.todo_id,
        )
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
            await coord._post_system_message(
                "**Planning failed:** Plan has no sub-tasks. Cannot execute."
            )
            await coord._transition_todo("failed", error_message="Plan produced 0 sub-tasks")
            return

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
        # Don't chain into execution directly — return and let the scheduler
        # pick up the in_progress state on the next dispatch. This avoids
        # dual execution paths (old ExecutionPhase vs scheduler._execute_jobs).
        logger.info("[%s] Plan approved, subtasks created. Returning to scheduler for execution.", coord.todo_id)
