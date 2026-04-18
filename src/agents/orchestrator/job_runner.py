"""Job runner — execution engine wrapping agents with lifecycle management.

Replaces ``agent_executor.py``.  Two entry points:

* ``run_llm_job()`` — for LLM-powered agents (coder, reviewer, tester, debugger).
  Handles prompt building, tool resolution, the LLM tool loop (single-shot or
  iterative RALPH), output validation, agent run records, token tracking,
  quality checks, and follow-up job spawning via ``agent.decide_spawn()``.

* ``run_procedural_job()`` — for non-LLM agents (pr_creator, merge, etc.).
  Simply calls ``agent.run()``.

Both wrap execution with lifecycle management: state transitions
(assigned -> running -> completed/failed), progress reporting.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agents.agents.registry import get_builtin_tool_schemas, get_default_tools
from agents.orchestrator.agent_result import AgentResult
from agents.utils.error_classification import classify_error, validate_debugger_output
from agents.orchestrator.output_validator import (
    build_correction_prompt,
    validate_agent_output,
    validate_agent_output_dict,
    MAX_VALIDATION_RETRIES,
)
from agents.orchestrator.structured_output import build_submit_tool_for_role
from agents.providers.base import AIProvider, run_tool_loop
from agents.schemas.agent import LLMMessage, LLMResponse

if TYPE_CHECKING:
    from agents.orchestrator.agents._base import BaseAgent, LLMAgent
    from agents.orchestrator.run_context import RunContext

logger = logging.getLogger(__name__)


# =====================================================================
# Tool resolution
# =====================================================================


async def _resolve_tools(
    ctx: RunContext,
    todo: dict,
    role: str,
    workspace_path: str | None,
    agent_config: dict | None = None,
) -> tuple[list[dict] | None, str]:
    """Resolve MCP tools + builtin tools for a role.

    Returns ``(tools_list, skills_context_str)``.
    """
    mcp_tools = await ctx.tools_registry.resolve_tools(
        project_id=str(todo["project_id"]),
        user_id=str(todo["creator_id"]),
    )
    skills_ctx = await ctx.tools_registry.build_skills_context(
        project_id=str(todo["project_id"]),
        user_id=str(todo["creator_id"]),
    )

    # Filter by role — custom agent config takes precedence
    if agent_config and agent_config.get("tools_enabled"):
        allowed = set(agent_config["tools_enabled"])
    else:
        allowed = set(get_default_tools(role))
    if mcp_tools:
        mcp_tools = [t for t in mcp_tools if t.get("name") in allowed]

    # Merge builtin workspace tools
    if workspace_path:
        builtin = get_builtin_tool_schemas(workspace_path, role)
        if mcp_tools:
            existing = {t["name"] for t in mcp_tools}
            mcp_tools.extend(t for t in builtin if t["name"] not in existing)
        else:
            mcp_tools = builtin

    return mcp_tools, skills_ctx or ""




# =====================================================================
# Quality checks
# =====================================================================


async def _run_quality_checks(
    ctx: RunContext,
    workspace_path: str,
    work_rules: dict,
    role: str,
    *,
    submit_data: dict | None = None,
) -> dict:
    """Run quality check commands in the workspace.

    Returns ``{"passed": bool, "reason": str, "error_output": str | None, "learnings": list}``.
    """
    if role == "debugger":
        return validate_debugger_output(submit_data)
    if role not in ("coder", "tester"):
        return {"passed": True, "reason": "not applicable", "learnings": []}

    quality_rules = work_rules.get("quality", [])
    if not quality_rules:
        return {"passed": True, "reason": "no quality rules", "learnings": []}

    # workspace_path IS the git working directory
    repo_dir = workspace_path

    all_passed = True
    combined_output: list[str] = []
    learnings: list[str] = []

    for cmd in quality_rules:
        try:
            exit_code, output = await ctx.workspace_mgr.run_command(cmd, repo_dir)
            if exit_code != 0:
                all_passed = False
                combined_output.append(f"[FAIL] {cmd}:\n{output[:500]}")
                learnings.append(f"Quality check failed: {cmd}")
            else:
                learnings.append(f"Quality check passed: {cmd}")
        except Exception as e:
            all_passed = False
            combined_output.append(f"[ERROR] {cmd}: {str(e)[:200]}")

    if all_passed:
        return {"passed": True, "reason": "all checks passed", "learnings": learnings}

    return {
        "passed": False,
        "reason": "failed_quality",
        "error_output": "\n".join(combined_output),
        "learnings": learnings,
    }


# =====================================================================
# Deliverables
# =====================================================================


async def _maybe_create_deliverable(
    ctx: RunContext,
    sub_task: dict,
    response: LLMResponse,
    run: dict,
    *,
    workspace_path: str | None = None,
) -> None:
    """Create a deliverable if the agent role produces one."""
    role = sub_task["agent_role"]
    if role not in ("coder", "report_writer"):
        return

    d_type = {"coder": "code_diff", "report_writer": "report"}.get(role, "document")

    # For coders: capture actual git diff from workspace
    diff_json = None
    if role == "coder" and workspace_path:
        try:
            diff_json = await _get_workspace_diff(workspace_path)
        except Exception:
            logger.warning("Failed to capture git diff for deliverable", exc_info=True)

    # Resolve target_repo_name for dependency deliverables
    dep_repo_name = None
    target_repo = sub_task.get("target_repo")
    if target_repo:
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo) if target_repo else None
        if isinstance(target_repo, dict) and target_repo.get("name"):
            dep_repo_name = target_repo["name"]

    await ctx.db.execute(
        """
        INSERT INTO deliverables (
            todo_id, agent_run_id, sub_task_id, type, title, content_md, content_json,
            target_repo_name
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        ctx.todo_id,
        run["id"],
        sub_task["id"],
        d_type,
        f"{d_type}: {sub_task['title']}",
        response.content,
        diff_json,
        dep_repo_name,
    )


async def _get_workspace_diff(workspace_path: str) -> dict | None:
    """Get git diff from workspace after coder commits.

    workspace_path IS the git working directory.
    """
    import asyncio

    async def _run(args: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode(errors="replace")

    rc, diff_output = await _run(["diff", "HEAD~1", "HEAD"])
    if rc != 0 or not diff_output.strip():
        return None

    _, stat_output = await _run(["diff", "--stat", "HEAD~1", "HEAD"])

    _, files_output = await _run(["diff", "--name-status", "HEAD~1", "HEAD"])
    files = []
    for line in files_output.strip().split("\n"):
        if line.strip():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                files.append({"status": parts[0], "path": parts[1]})

    return {
        "diff": diff_output[:100_000],
        "stats": stat_output.strip(),
        "files": files,
    }


# =====================================================================
# Agent config resolution
# =====================================================================


async def _resolve_agent_config(ctx: RunContext, role: str, owner_id: str) -> dict | None:
    """Look up a custom agent_config for the given role.

    Returns the row dict if a matching active config exists, else None.
    """
    row = await ctx.db.fetchrow(
        "SELECT * FROM agent_configs WHERE role = $1 AND owner_id = $2 AND is_active = TRUE "
        "ORDER BY updated_at DESC LIMIT 1",
        role,
        owner_id,
    )
    return dict(row) if row else None


# =====================================================================
# Progress log persistence
# =====================================================================


async def _append_progress_log(
    ctx: RunContext,
    sub_task: dict,
    iterations_used: int,
    outcome: str,
    iteration_log: list[dict],
) -> None:
    """Append a sub-task completion record to todo_items.progress_log."""
    key_learnings: list[str] = []
    for entry in iteration_log:
        for learning in entry.get("learnings", []):
            if learning not in key_learnings:
                key_learnings.append(learning)
    key_learnings = key_learnings[-10:]

    record = {
        "sub_task_id": str(sub_task["id"]),
        "sub_task_title": sub_task["title"],
        "iterations_used": iterations_used,
        "outcome": outcome,
        "key_learnings": key_learnings,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    await ctx.db.execute(
        """
        UPDATE todo_items
        SET progress_log = COALESCE(progress_log, '[]'::jsonb) || $2,
            updated_at = NOW()
        WHERE id = $1
        """,
        ctx.todo_id,
        [record],
    )


async def _persist_execution_events(
    ctx: RunContext, subtask_id: str, events: list[dict],
) -> None:
    """Save accumulated execution events to the sub_tasks table."""
    if not events:
        return
    try:
        await ctx.db.execute(
            "UPDATE sub_tasks SET execution_events = $2 WHERE id = $1",
            subtask_id, events,
        )
    except Exception:
        logger.debug(
            "[%s] Failed to persist execution events for %s",
            ctx.todo_id[:8], str(subtask_id)[:8], exc_info=True,
        )


# =====================================================================
# Create subtask tool handler
# =====================================================================


async def _handle_create_subtask_tool(
    ctx: RunContext,
    parent_subtask: dict,
    args: dict,
    workspace_path: str | None,
) -> str:
    """Handle the create_subtask builtin tool called by a coder agent."""
    title = args.get("title", "").strip()
    description = args.get("description", "").strip()
    agent_role = args.get("agent_role", "coder").strip()

    if not title:
        return json.dumps({"error": "title is required"})
    if not description:
        return json.dumps({"error": "description is required"})
    if agent_role not in ("coder", "tester", "reviewer", "debugger"):
        return json.dumps({
            "error": f"Invalid agent_role: {agent_role}. Use coder, tester, reviewer, or debugger.",
        })

    parent_order = parent_subtask.get("execution_order") or 0

    try:
        row = await ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            ctx.todo_id,
            title,
            description,
            agent_role,
            parent_order,
            [],
        )
        st_id = str(row["id"])
        logger.info(
            "[%s] Agent created child subtask %s: role=%s title=%s (parent=%s)",
            ctx.todo_id, st_id, agent_role, title, parent_subtask["id"],
        )
        await ctx.post_system_message(
            f"**Subtask created by {parent_subtask['agent_role']}:** "
            f"[{agent_role}] {title}"
        )
        return json.dumps({
            "status": "created",
            "subtask_id": st_id,
            "title": title,
            "agent_role": agent_role,
            "message": "Subtask created. It will be executed after current batch completes.",
        })
    except Exception as e:
        logger.error("[%s] Failed to create child subtask: %s", ctx.todo_id, e)
        return json.dumps({"error": f"Failed to create subtask: {str(e)}"})


# =====================================================================
# Tool event publishing helpers
# =====================================================================


def _make_tool_event_handler(
    ctx: RunContext,
    st_id: str,
    accumulated_events: list[dict],
    workspace_path: str | None = None,
):
    """Build the on_tool_event callback for tool loop execution."""

    async def _on_tool_event(event: dict) -> None:
        try:
            event["sub_task_id"] = st_id
            event["ts"] = time.time()
            await ctx.redis.publish(
                f"task:{ctx.todo_id}:events",
                json.dumps(event),
            )
            # Accumulate for DB persistence (cap at 2000 events)
            if len(accumulated_events) < 2000:
                accumulated_events.append(event)
            # Publish extra index_search event when semantic_search completes
            if (
                event.get("type") == "tool_result"
                and event.get("name") == "semantic_search"
                and workspace_path
            ):
                try:
                    from agents.indexing.search_tool import pop_last_search_meta

                    _idx_dir = os.path.join(workspace_path, ".agent_index")
                    search_meta = pop_last_search_meta(_idx_dir)
                    if search_meta:
                        idx_event = {
                            "type": "index_search",
                            "sub_task_id": st_id,
                            "ts": time.time(),
                            **search_meta,
                        }
                        await ctx.redis.publish(
                            f"task:{ctx.todo_id}:events",
                            json.dumps(idx_event),
                        )
                        if len(accumulated_events) < 2000:
                            accumulated_events.append(idx_event)
                except Exception:
                    pass
        except Exception:
            pass  # Don't let event publishing break execution

    return _on_tool_event


def _make_inject_checker(ctx: RunContext, st_id: str):
    """Build the on_inject_check callback for polling user-injected messages."""
    inject_key = f"subtask:{st_id}:inject"

    async def _check_inject() -> str | None:
        if ctx.redis:
            return await ctx.redis.lpop(inject_key)
        return None

    return _check_inject, inject_key


# =====================================================================
# Single-shot LLM execution
# =====================================================================


async def _execute_single_shot(
    agent: LLMAgent,
    job: dict,
    ctx: RunContext,
    provider: AIProvider,
    *,
    workspace_path: str | None = None,
) -> tuple[dict, LLMResponse, dict]:
    """Run a single-shot LLM execution (no iteration loop).

    Returns ``(validated_output, response, run_record)``.
    """
    st_id = str(job["id"])
    role = job["agent_role"]
    todo = await ctx.load_todo()

    # Resolve custom agent config
    agent_config = await _resolve_agent_config(ctx, role, str(todo["creator_id"]))

    # Model preference: agent config > task-level > provider default
    model_override = None
    if agent_config and agent_config.get("model_preference"):
        model_override = agent_config["model_preference"]
    elif todo.get("ai_model"):
        model_override = todo["ai_model"]

    # Create agent run record
    run = await ctx.db.fetchrow(
        """
        INSERT INTO agent_runs (
            todo_id, sub_task_id, agent_role, agent_model, provider_type
        )
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        ctx.todo_id,
        job["id"],
        role,
        provider.default_model,
        provider.provider_type,
    )

    start_time = time.monotonic()

    try:
        await ctx.report_progress(st_id, 10, f"Starting: {job['title']}")

        # Build prompt via the agent
        prompt = await agent.build_prompt(
            job, workspace_path, ctx, todo,
            agent_config=agent_config,
        )

        # Resolve tools
        mcp_tools, skills_ctx = await _resolve_tools(
            ctx, todo, role, workspace_path, agent_config=agent_config,
        )

        # Append skills context to system prompt
        system_prompt = prompt["system"]
        if skills_ctx:
            system_prompt += skills_ctx

        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=prompt["user"]),
        ]
        tools_arg = mcp_tools if mcp_tools else None

        # Roles that write code need more output tokens
        role_max_tokens = 16384 if role in ("coder", "tester") else 8192

        # Structured output via submit_result tool
        _submit_tool = build_submit_tool_for_role(role)
        _submit_result_data: dict | None = None
        if _submit_tool and tools_arg:
            tools_arg = [t for t in tools_arg if t["name"] != "task_complete"]
            tools_arg.append(_submit_tool)

        send_kwargs: dict[str, Any] = {"temperature": 0.1, "max_tokens": role_max_tokens}
        if model_override:
            send_kwargs["model"] = model_override

        logger.info(
            "[%s] st=%s Sending LLM request (tools=%d, model_override=%s)",
            ctx.todo_id, st_id, len(tools_arg) if tools_arg else 0, model_override,
        )

        async def _on_tool_round(round_num: int, resp: LLMResponse) -> None:
            await ctx.report_progress(
                st_id, 10 + round_num * 6,
                f"Using tool: {resp.tool_calls[0].get('name', '?') if resp.tool_calls else '?'}",
            )

        async def _tool_exec(name: str, args: dict) -> str:
            nonlocal _submit_result_data
            if name == "submit_result":
                _submit_result_data = args
                return "Structured output received. Task complete."
            if name == "create_subtask":
                return await _handle_create_subtask_tool(ctx, job, args, workspace_path)
            return await ctx.mcp_executor.execute_tool(name, args, mcp_tools)

        _accumulated_events: list[dict] = []
        _on_tool_event = _make_tool_event_handler(
            ctx, st_id, _accumulated_events, workspace_path,
        )
        _check_inject, _inject_key = _make_inject_checker(ctx, st_id)

        # Wire up command output streaming to Redis
        async def _stream_cmd_output(line: str) -> None:
            try:
                await ctx.redis.publish(
                    f"task:{ctx.todo_id}:events",
                    json.dumps({
                        "type": "command_output",
                        "sub_task_id": st_id,
                        "line": line,
                    }),
                )
            except Exception:
                pass
        ctx.mcp_executor.on_command_output = _stream_cmd_output

        _token_cb = ctx.build_token_streamer(st_id)
        content, response = await run_tool_loop(
            provider, messages,
            tools=tools_arg,
            tool_executor=_tool_exec,
            max_rounds=500,
            on_tool_round=_on_tool_round,
            on_activity=lambda msg: ctx.report_activity(st_id, msg),
            on_tool_event=_on_tool_event,
            on_inject_check=_check_inject,
            on_cancel_check=ctx.is_cancelled,
            on_token=_token_cb,
            **send_kwargs,
        )
        if hasattr(_token_cb, "flush"):
            await _token_cb.flush()

        logger.info(
            "[%s] st=%s Tool loop done: content_len=%d stop=%s",
            ctx.todo_id, st_id, len(content) if content else 0, response.stop_reason,
        )

        # Drain unconsumed inject messages
        if ctx.redis:
            while await ctx.redis.lpop(_inject_key):
                pass

        await ctx.report_progress(st_id, 80, f"Processing output: {job['title']}")

        # -- Structured output validation --
        validated_output = None
        raw_content = content or response.content or ""

        # If submit_result was called, validate the dict directly
        if _submit_result_data is not None:
            validated_output, val_errors = validate_agent_output_dict(
                role, dict(_submit_result_data), raw_content,
            )
            if validated_output is None:
                logger.warning(
                    "submit_result validation failed for %s (%s): %s — using raw dict",
                    st_id, role, val_errors,
                )
                validated_output = dict(_submit_result_data)
                validated_output.setdefault("content", raw_content)

        # Fallback: single text-based extraction attempt (no retry loop)
        if validated_output is None:
            validated_output, val_errors = validate_agent_output(role, response.content)

        if validated_output is None:
            logger.warning(
                "Subtask %s (%s) output validation failed: %s — using raw content",
                st_id, role, val_errors,
            )
            validated_output = {"content": response.content, "raw_content": response.content}

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Update agent run
        await ctx.db.execute(
            """
            UPDATE agent_runs
            SET status = 'completed', output_result = $2,
                tokens_input = $3, tokens_output = $4,
                duration_ms = $5, cost_usd = $6, completed_at = NOW()
            WHERE id = $1
            """,
            run["id"],
            validated_output,
            response.tokens_input,
            response.tokens_output,
            duration_ms,
            response.cost_usd,
        )

        # Transition subtask to completed
        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100,
            progress_message="Done",
            output_result=validated_output,
        )

        # Create deliverable if applicable
        await _maybe_create_deliverable(
            ctx, job, response, run, workspace_path=workspace_path,
        )

        # Track tokens
        await ctx.track_tokens(response)
        await ctx.report_progress(st_id, 100, f"Completed: {job['title']}")

        return validated_output, response, dict(run)

    except Exception as e:
        import traceback as _tb

        duration_ms = int((time.monotonic() - start_time) * 1000)
        error_type = classify_error(e)
        error_detail = f"{type(e).__name__}: {e}"
        error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

        await ctx.db.execute(
            """
            UPDATE agent_runs
            SET status = 'failed', error_type = $2,
                error_detail = $3, duration_ms = $4, completed_at = NOW()
            WHERE id = $1
            """,
            run["id"],
            error_type,
            error_detail + "\n\n" + error_traceback[-2000:],
            duration_ms,
        )
        raise


# =====================================================================
# Iterative (RALPH) LLM execution
# =====================================================================


async def _execute_iterative(
    agent: LLMAgent,
    job: dict,
    ctx: RunContext,
    provider: AIProvider,
    *,
    workspace_path: str | None = None,
    work_rules: dict | None = None,
    max_iterations: int = 500,
) -> tuple[dict, LLMResponse | None, dict | None]:
    """RALPH-style iterative execution: fresh context per iteration,
    quality checks, stuck detection, hard cutoff.

    Returns ``(validated_output, last_response, last_run_record)``.
    """
    from agents.orchestrator.context_builder import ContextBuilder

    st_id = str(job["id"])
    role = job["agent_role"]

    iteration_log: list[dict] = []
    _accumulated_events: list[dict] = []
    agent_signaled_done = False
    agent_done_summary: str | None = None
    qc_retries_after_done = 0
    _MAX_QC_RETRIES_AFTER_DONE = 2

    todo = await ctx.load_todo()
    agent_config = await _resolve_agent_config(ctx, role, str(todo["creator_id"]))

    # Role-specific work rules
    role_rules = ContextBuilder.filter_rules_for_role(work_rules or {}, role)
    has_quality_rules = bool(role_rules.get("quality"))

    # Model override resolution: agent config > task-level > provider default
    model_override = None
    if agent_config and agent_config.get("model_preference"):
        model_override = agent_config["model_preference"]
    elif todo.get("ai_model"):
        model_override = todo["ai_model"]

    # Architect/Editor dual-model config
    _ae_project = await ctx.db.fetchrow(
        "SELECT architect_editor_enabled, architect_model, editor_model FROM projects WHERE id = $1",
        todo["project_id"],
    )
    architect_editor_enabled = bool(_ae_project and _ae_project.get("architect_editor_enabled"))
    architect_model = (_ae_project or {}).get("architect_model")
    editor_model = (_ae_project or {}).get("editor_model")

    # Pre-resolve tools ONCE (avoid repeated DB queries per iteration)
    mcp_tools, skills_ctx = await _resolve_tools(
        ctx, todo, role, workspace_path, agent_config=agent_config,
    )

    # Cache repo map text across iterations
    _cached_repo_map: str | None = None

    last_response: LLMResponse | None = None
    last_run: dict | None = None

    for iteration in range(1, max_iterations + 1):
        # Check for cancellation
        if await ctx.is_cancelled():
            logger.info(
                "[%s] Task cancelled during iteration %d of %s, aborting",
                ctx.todo_id, iteration, st_id,
            )
            return (
                {"content": "cancelled", "_cancelled": True},
                last_response,
                last_run,
            )

        start_time = time.monotonic()

        # Emit iteration_start event
        try:
            _iter_start_evt = {
                "type": "iteration_start",
                "sub_task_id": st_id,
                "iteration": iteration,
                "subtask": job["title"],
                "ts": time.time(),
            }
            await ctx.redis.publish(
                f"task:{ctx.todo_id}:events",
                json.dumps(_iter_start_evt),
            )
            if len(_accumulated_events) < 2000:
                _accumulated_events.append(_iter_start_evt)
        except Exception:
            pass

        # Create agent run record
        run = await ctx.db.fetchrow(
            """
            INSERT INTO agent_runs (
                todo_id, sub_task_id, agent_role, agent_model, provider_type
            )
            VALUES ($1, $2, $3, $4, $5) RETURNING *
            """,
            ctx.todo_id,
            job["id"],
            role,
            provider.default_model,
            provider.provider_type,
        )
        last_run = dict(run)

        try:
            await ctx.report_progress(
                st_id,
                min(10 + iteration * 2, 80),
                f"Iteration {iteration}: {job['title']}",
            )

            # 1. Build FRESH context (no carried conversation)
            prompt = await agent.build_prompt(
                job, workspace_path, ctx, todo,
                iteration=iteration,
                iteration_log=iteration_log,
                work_rules=role_rules,
                agent_config=agent_config,
                cached_repo_map=_cached_repo_map,
            )

            system_prompt = prompt["system"]
            if skills_ctx:
                system_prompt += skills_ctx

            # 2. Execute single iteration (one LLM call + tool loop)
            messages = [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=prompt["user"]),
            ]
            tools_arg = list(mcp_tools) if mcp_tools else None

            # Add submit_result tool
            _submit_tool = build_submit_tool_for_role(role)
            _submit_result_data: dict | None = None
            if _submit_tool and tools_arg:
                tools_arg = [t for t in tools_arg if t["name"] != "task_complete"]
                tools_arg.append(_submit_tool)

            send_kwargs: dict[str, Any] = {"temperature": 0.1, "max_tokens": 16384}
            if model_override:
                send_kwargs["model"] = model_override

            async def _iter_tool_exec(name: str, args: dict) -> str:
                nonlocal agent_signaled_done, agent_done_summary, _submit_result_data
                if name == "submit_result":
                    agent_signaled_done = True
                    _submit_result_data = args
                    agent_done_summary = args.get("summary", args.get("approach", ""))
                    return "Structured output received. Task complete."
                if name == "task_complete":
                    agent_signaled_done = True
                    agent_done_summary = args.get("summary", "")
                    return "Task completion acknowledged. Wrapping up."
                if name == "create_subtask":
                    return await _handle_create_subtask_tool(
                        ctx, job, args, workspace_path,
                    )
                return await ctx.mcp_executor.execute_tool(name, args, mcp_tools)

            _on_tool_event = _make_tool_event_handler(
                ctx, st_id, _accumulated_events, workspace_path,
            )
            _check_inject, _ = _make_inject_checker(ctx, st_id)

            # Wire up command output streaming
            async def _iter_stream_cmd(line: str) -> None:
                try:
                    await ctx.redis.publish(
                        f"task:{ctx.todo_id}:events",
                        json.dumps({"type": "command_output", "sub_task_id": st_id, "line": line}),
                    )
                except Exception:
                    pass
            ctx.mcp_executor.on_command_output = _iter_stream_cmd

            # Architect/Editor dual-model execution
            if architect_editor_enabled and architect_model and editor_model:
                READ_ONLY_TOOLS = {"read_file", "list_directory", "search_files", "semantic_search"}
                WRITE_TOOLS = {"write_file", "edit_file", "run_command", "task_complete", "create_subtask"}

                # Phase A: Architect — powerful model with read-only tools
                architect_tools = [t for t in (tools_arg or []) if t["name"] in READ_ONLY_TOOLS]
                architect_kwargs = {**send_kwargs, "model": architect_model}

                async def _architect_tool_exec(name: str, args: dict) -> str:
                    return await ctx.mcp_executor.execute_tool(name, args, mcp_tools)

                _arch_token_cb = ctx.build_token_streamer(st_id)
                architect_content, architect_response = await run_tool_loop(
                    provider, list(messages),
                    tools=architect_tools or None,
                    tool_executor=_architect_tool_exec,
                    max_rounds=500,
                    on_activity=lambda msg, _i=iteration: ctx.report_activity(
                        st_id, f"[iter {_i}] [Architect] {msg}",
                    ),
                    on_tool_event=_on_tool_event,
                    on_inject_check=_check_inject,
                    on_cancel_check=ctx.is_cancelled,
                    on_token=_arch_token_cb,
                    **architect_kwargs,
                )
                if hasattr(_arch_token_cb, "flush"):
                    await _arch_token_cb.flush()

                # Phase B: Editor — fast model with write tools
                editor_tools = [t for t in (tools_arg or []) if t["name"] in WRITE_TOOLS]
                editor_system = (
                    system_prompt
                    + "\n\n## Architect's Analysis & Plan\n"
                    + "Follow the architect's instructions below to make the required changes.\n\n"
                    + (architect_content or architect_response.content or "No architect output.")
                )
                editor_messages = [
                    LLMMessage(role="system", content=editor_system),
                    LLMMessage(role="user", content=(
                        "Apply the changes described in the architect's plan above. "
                        "Use the available write tools to implement the changes."
                    )),
                ]
                editor_kwargs = {**send_kwargs, "model": editor_model}

                _edit_token_cb = ctx.build_token_streamer(st_id)
                iter_content, response = await run_tool_loop(
                    provider, editor_messages,
                    tools=editor_tools or None,
                    tool_executor=_iter_tool_exec,
                    max_rounds=500,
                    on_activity=lambda msg, _i=iteration: ctx.report_activity(
                        st_id, f"[iter {_i}] [Editor] {msg}",
                    ),
                    on_tool_event=_on_tool_event,
                    on_inject_check=_check_inject,
                    on_cancel_check=ctx.is_cancelled,
                    on_token=_edit_token_cb,
                    **editor_kwargs,
                )
                if hasattr(_edit_token_cb, "flush"):
                    await _edit_token_cb.flush()

                # Combine token usage from both phases
                response.tokens_input += architect_response.tokens_input
                response.tokens_output += architect_response.tokens_output
                if response.cost_usd and architect_response.cost_usd:
                    response.cost_usd += architect_response.cost_usd
            else:
                # Standard single-model execution
                _std_token_cb = ctx.build_token_streamer(st_id)
                iter_content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_arg,
                    tool_executor=_iter_tool_exec,
                    max_rounds=500,
                    on_activity=lambda msg, _i=iteration: ctx.report_activity(
                        st_id, f"[iter {_i}] {msg}",
                    ),
                    on_tool_event=_on_tool_event,
                    on_inject_check=_check_inject,
                    on_cancel_check=ctx.is_cancelled,
                    on_token=_std_token_cb,
                    **send_kwargs,
                )
                if hasattr(_std_token_cb, "flush"):
                    await _std_token_cb.flush()

            last_response = response

            # Detect tool loop truncation
            tool_loop_truncated = response.stop_reason == "max_tool_rounds"
            if tool_loop_truncated:
                logger.warning(
                    "[%s] Tool loop truncated at max_rounds for subtask %s iteration %d",
                    ctx.todo_id, st_id, iteration,
                )

            # Broadcast LLM text response for UI
            if response.content and response.content.strip():
                preview = response.content.strip()
                if len(preview) > 500:
                    preview = preview[:500] + "..."
                await ctx.redis.publish(
                    f"task:{ctx.todo_id}:progress",
                    json.dumps({
                        "type": "llm_response",
                        "sub_task_id": st_id,
                        "iteration": iteration,
                        "content": response.content.strip(),
                        "preview": preview,
                    }),
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)
            total_tokens = response.tokens_input + response.tokens_output

            # Update agent run
            await ctx.db.execute(
                """
                UPDATE agent_runs
                SET status = 'completed', output_result = $2,
                    tokens_input = $3, tokens_output = $4,
                    duration_ms = $5, cost_usd = $6, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                {"content": response.content},
                response.tokens_input,
                response.tokens_output,
                duration_ms,
                response.cost_usd,
            )

            # Track tokens
            await ctx.track_tokens(response)

            # 3. Run quality checks
            if has_quality_rules and workspace_path:
                qc_result = await _run_quality_checks(
                    ctx, workspace_path, role_rules, role,
                    submit_data=_submit_result_data,
                )
            elif role == "debugger" and agent_signaled_done and _submit_result_data:
                qc_result = validate_debugger_output(_submit_result_data)
            else:
                qc_result = {"passed": True, "reason": "no quality rules", "learnings": []}

            # Report QC result as activity
            if has_quality_rules and workspace_path:
                if qc_result["passed"]:
                    await ctx.report_activity(st_id, f"[iter {iteration}] Quality check passed")
                else:
                    reason = qc_result.get("reason", "failed")
                    await ctx.report_activity(
                        st_id, f"[iter {iteration}] Quality check failed: {reason}",
                    )

            # 4. Record iteration
            action = "implement" if iteration == 1 else "fix"
            learnings = qc_result.get("learnings", [])
            if tool_loop_truncated:
                learnings.append(
                    "Tool loop was truncated (hit max_rounds). "
                    "Agent may not have finished all intended tool calls."
                )
            entry: dict[str, Any] = {
                "iteration": iteration,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "outcome": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                "error_output": qc_result.get("error_output") if not qc_result["passed"] else None,
                "learnings": learnings,
                "files_changed": [],
                "stuck_check": None,
                "tokens_used": total_tokens,
                "tool_loop_truncated": tool_loop_truncated,
                "llm_response": response.content.strip()[:500] if response.content else None,
            }
            iteration_log.append(entry)

            # Persist iteration log
            await ctx.db.execute(
                "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                job["id"],
                iteration_log,
            )

            # Emit iteration_end event
            try:
                _iter_end_evt = {
                    "type": "iteration_end",
                    "sub_task_id": st_id,
                    "iteration": iteration,
                    "status": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                    "ts": time.time(),
                }
                await ctx.redis.publish(
                    f"task:{ctx.todo_id}:events",
                    json.dumps(_iter_end_evt),
                )
                if len(_accumulated_events) < 2000:
                    _accumulated_events.append(_iter_end_evt)
            except Exception:
                pass

            # 5. Quality checks passed -> validate output then DONE
            if qc_result["passed"]:
                validated_output = None
                for val_attempt in range(MAX_VALIDATION_RETRIES + 1):
                    validated_output, val_errors = validate_agent_output(
                        role, response.content,
                    )
                    if validated_output is not None:
                        break
                    if val_attempt < MAX_VALIDATION_RETRIES:
                        correction = build_correction_prompt(role, val_errors, response.content)
                        messages.append(LLMMessage(role="assistant", content=response.content))
                        messages.append(LLMMessage(role="user", content=correction))
                        response = await provider.send_message(
                            messages, temperature=0.1, tools=tools_arg,
                            **({"model": model_override} if model_override else {}),
                        )

                if validated_output is None:
                    logger.warning(
                        "RALPH subtask %s (%s) failed output validation after %d retries: %s",
                        st_id, role, MAX_VALIDATION_RETRIES, val_errors,
                    )
                    validated_output = {
                        "content": response.content,
                        "raw_content": response.content,
                        "_validation_failed": True,
                        "_validation_errors": val_errors,
                    }

                await ctx.transition_subtask(
                    st_id, "completed",
                    progress_pct=100,
                    progress_message="Done",
                    output_result=validated_output,
                )
                await _maybe_create_deliverable(
                    ctx, job, response, run, workspace_path=workspace_path,
                )
                await _append_progress_log(
                    ctx, job, iteration, "completed", iteration_log,
                )
                await _persist_execution_events(subtask_id=st_id, ctx=ctx, events=_accumulated_events)
                await ctx.report_progress(st_id, 100, f"Completed: {job['title']}")
                return validated_output, response, dict(run)

            # 5b. Agent signaled done via task_complete / submit_result
            if agent_signaled_done:
                if not qc_result["passed"] and qc_retries_after_done < _MAX_QC_RETRIES_AFTER_DONE:
                    qc_retries_after_done += 1
                    logger.warning(
                        "[%s] Agent signaled task_complete on iteration %d but QC failed "
                        "(retry %d/%d): %s",
                        ctx.todo_id, iteration, qc_retries_after_done,
                        _MAX_QC_RETRIES_AFTER_DONE, qc_result.get("reason", "unknown"),
                    )
                    entry["outcome"] = f"agent_done_but_qc_failed ({qc_result.get('reason', 'unknown')})"
                    entry["error_output"] = qc_result.get("error_output")
                    entry["learnings"].append(
                        "Agent signaled task_complete but quality checks did not pass. "
                        "Continuing iteration to fix quality issues."
                    )
                    iteration_log[-1] = entry
                    await ctx.db.execute(
                        "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                        job["id"], iteration_log,
                    )
                    # Reset signal so the loop continues
                    agent_signaled_done = False
                    agent_done_summary = ""
                    continue

                # QC passed (or retries exhausted) — accept the result
                if not qc_result["passed"]:
                    logger.warning(
                        "[%s] Agent signaled task_complete, QC still failing after %d retries — "
                        "accepting with warning",
                        ctx.todo_id, _MAX_QC_RETRIES_AFTER_DONE,
                    )
                logger.info(
                    "[%s] Agent signaled task_complete on iteration %d (qc=%s)",
                    ctx.todo_id, iteration,
                    "passed" if qc_result["passed"] else "failed_exhausted",
                )

                validated_output, _ = validate_agent_output(role, response.content)
                if validated_output is None:
                    validated_output = {
                        "content": response.content,
                        "raw_content": response.content,
                        "summary": agent_done_summary,
                    }
                else:
                    validated_output["summary"] = agent_done_summary

                if not qc_result["passed"]:
                    validated_output["_qc_failed"] = True
                    validated_output["_qc_reason"] = qc_result.get("reason", "unknown")

                await ctx.transition_subtask(
                    st_id, "completed",
                    progress_pct=100,
                    progress_message=agent_done_summary or "Done",
                    output_result=validated_output,
                )
                await _maybe_create_deliverable(
                    ctx, job, response, run, workspace_path=workspace_path,
                )
                await _append_progress_log(
                    ctx, job, iteration, "completed_agent_signal", iteration_log,
                )
                await _persist_execution_events(subtask_id=st_id, ctx=ctx, events=_accumulated_events)
                await ctx.report_progress(st_id, 100, f"Completed: {job['title']}")
                return validated_output, response, dict(run)

        except Exception as e:
            import traceback as _tb

            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_type = classify_error(e)
            error_detail = f"{type(e).__name__}: {e}"
            error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

            await ctx.db.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error_type = $2,
                    error_detail = $3, duration_ms = $4, completed_at = NOW()
                WHERE id = $1
                """,
                run["id"],
                error_type,
                error_detail + "\n\n" + error_traceback[-2000:],
                duration_ms,
            )
            # Record failure in iteration log
            iteration_log.append({
                "iteration": iteration,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "error",
                "outcome": f"exception: {error_detail[:500]}",
                "error_output": error_traceback[-3000:],
                "learnings": [],
                "tokens_used": 0,
            })
            await ctx.db.execute(
                "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                job["id"],
                iteration_log,
            )
            # Report error as activity
            await ctx.redis.publish(
                f"task:{ctx.todo_id}:progress",
                json.dumps({
                    "type": "activity",
                    "sub_task_id": st_id,
                    "activity": f"ERROR: {error_detail[:300]}",
                }),
            )
            raise

    # Hard cutoff — max_iterations reached
    await ctx.post_system_message(
        f"**Sub-task failed after {max_iterations} iterations:** {job['title']}"
    )
    await _append_progress_log(
        ctx, job, max_iterations, "failed_max_iterations", iteration_log,
    )
    await _persist_execution_events(subtask_id=st_id, ctx=ctx, events=_accumulated_events)
    await ctx.transition_subtask(
        st_id, "failed",
        error_message=f"Max iterations ({max_iterations}) reached without passing quality checks",
    )
    return (
        {"content": "max iterations reached", "_max_iterations": True},
        last_response,
        last_run,
    )


# =====================================================================
# Public API
# =====================================================================


async def run_llm_job(
    agent: LLMAgent,
    job: dict,
    ctx: RunContext,
    provider: AIProvider,
    *,
    workspace_path: str | None = None,
    work_rules: dict | None = None,
    max_iterations: int = 500,
) -> AgentResult:
    """Execute an LLM-powered agent job with full lifecycle management.

    Handles:
      - State transitions (assigned -> running -> completed/failed)
      - Prompt building via ``agent.build_prompt()``
      - Tool resolution (MCP + builtin)
      - LLM tool loop (single-shot or iterative RALPH)
      - Output validation
      - Agent run records
      - Token tracking
      - Quality checks (for iterative)
      - Follow-up job spawning via ``agent.decide_spawn()``

    Parameters
    ----------
    agent : LLMAgent
        The agent instance whose build_prompt/decide_spawn methods drive execution.
    job : dict
        The sub_tasks row (all columns).
    ctx : RunContext
        Infrastructure services bag.
    provider : AIProvider
        The resolved AI provider for this task.
    workspace_path : str | None
        Path to the task workspace.
    work_rules : dict | None
        Project work rules (quality, style, etc.).
    max_iterations : int
        Max iterations for RALPH loop (only used in iterative mode).

    Returns
    -------
    AgentResult
        Contains output dict and spawn declarations for follow-up jobs.
    """
    st_id = str(job["id"])
    role = job["agent_role"]

    logger.info(
        "[%s] run_llm_job START: st=%s role=%s title=%s workspace=%s",
        ctx.todo_id, st_id, role, job["title"], workspace_path,
    )

    # Lifecycle: assigned -> running
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    # Update parent TODO sub_state
    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
        role,
    )

    # Determine execution mode: iterative (has quality rules) or single-shot
    from agents.orchestrator.context_builder import ContextBuilder

    role_rules = ContextBuilder.filter_rules_for_role(work_rules or {}, role)
    has_quality_rules = bool(role_rules.get("quality"))
    use_iterative = has_quality_rules or role in ("coder", "tester", "debugger")

    if use_iterative:
        validated_output, response, run = await _execute_iterative(
            agent, job, ctx, provider,
            workspace_path=workspace_path,
            work_rules=work_rules,
            max_iterations=max_iterations,
        )
    else:
        validated_output, response, run = await _execute_single_shot(
            agent, job, ctx, provider,
            workspace_path=workspace_path,
        )

    # Call agent.decide_spawn() for follow-up jobs
    spawn = agent.decide_spawn(job, validated_output)

    return AgentResult(
        output=validated_output,
        spawn=spawn,
    )


async def run_procedural_job(
    agent: BaseAgent,
    job: dict,
    ctx: RunContext,
    *,
    workspace_path: str | None = None,
) -> AgentResult:
    """Execute a non-LLM (procedural) agent job with lifecycle management.

    Simply calls ``agent.run()`` wrapped with state transitions and
    progress reporting.

    Parameters
    ----------
    agent : BaseAgent
        The procedural agent instance.
    job : dict
        The sub_tasks row (all columns).
    ctx : RunContext
        Infrastructure services bag.
    workspace_path : str | None
        Path to the task workspace.

    Returns
    -------
    AgentResult
        The result from ``agent.run()``.
    """
    st_id = str(job["id"])
    role = job["agent_role"]

    logger.info(
        "[%s] run_procedural_job START: st=%s role=%s title=%s",
        ctx.todo_id, st_id, role, job["title"],
    )

    # Lifecycle: assigned -> running
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    # Update parent TODO sub_state
    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
        role,
    )

    start_time = time.monotonic()

    try:
        await ctx.report_progress(st_id, 10, f"Starting: {job['title']}")

        result = await agent.run(job, workspace_path, ctx)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100,
            progress_message="Done",
            output_result=result.output,
        )
        await ctx.report_progress(st_id, 100, f"Completed: {job['title']}")

        logger.info(
            "[%s] run_procedural_job DONE: st=%s role=%s duration=%dms",
            ctx.todo_id, st_id, role, duration_ms,
        )

        return result

    except Exception as e:
        import traceback as _tb

        duration_ms = int((time.monotonic() - start_time) * 1000)
        error_type = classify_error(e)
        error_detail = f"{type(e).__name__}: {e}"

        logger.error(
            "[%s] run_procedural_job FAILED: st=%s role=%s error=%s duration=%dms",
            ctx.todo_id, st_id, role, error_detail, duration_ms,
        )

        await ctx.transition_subtask(
            st_id, "failed",
            error_message=error_detail[:500],
        )
        raise
