"""Subtask execution — single-shot and iterative (RALPH) modes.

Extracted from the coordinator God Object so the execution logic can be
tested and evolved independently.  The ``AgentExecutor`` delegates back
to the coordinator for infrastructure helpers (progress reporting, state
transitions, token streaming, etc.) via a back-reference.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agents.agents.registry import get_builtin_tool_schemas, get_default_tools
from agents.schemas.agent import LLMMessage, LLMResponse
from agents.providers.base import AIProvider

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator

logger = logging.getLogger(__name__)


def _classify_error(exc: Exception) -> str:
    """Classify an exception into a category for the ``error_type`` field."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(k in name for k in ("timeout", "timedout")):
        return "timeout"
    if any(k in name for k in ("ratelimit", "rate_limit", "429")):
        return "rate_limit"
    if "401" in msg or "unauthorized" in msg or "authentication" in msg:
        return "auth_error"
    if "context" in msg and ("long" in msg or "length" in msg or "tokens" in msg):
        return "context_length"
    if any(k in name for k in ("connection", "network", "dns")):
        return "network"
    if "json" in name or "parse" in name or "decode" in name:
        return "parse_error"
    return "transient"


class AgentExecutor:
    """Runs individual subtasks — either single-shot or iteratively.

    Parameters
    ----------
    coord : AgentCoordinator
        Back-reference to the coordinator that owns this executor.
        Used to access shared infrastructure (DB, Redis, state helpers).
    """

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord
        # Shorthand aliases for frequently accessed attributes
        self.db = coord.db
        self.redis = coord.redis
        self.todo_id = coord.todo_id
        self.mcp_executor = coord.mcp_executor
        self.tools_registry = coord.tools_registry
        self.workspace_mgr = coord.workspace_mgr

    # ------------------------------------------------------------------
    # Helpers (delegated to coordinator)
    # ------------------------------------------------------------------

    async def _transition_subtask(self, *args: Any, **kwargs: Any) -> Any:
        return await self._coord._transition_subtask(*args, **kwargs)

    async def _report_progress(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._report_progress(*args, **kwargs)

    async def _report_activity(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._report_activity(*args, **kwargs)

    def _build_token_streamer(self, *args: Any, **kwargs: Any) -> Any:
        return self._coord._build_token_streamer(*args, **kwargs)

    async def _track_tokens(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._track_tokens(*args, **kwargs)

    async def _maybe_create_deliverable(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._maybe_create_deliverable(*args, **kwargs)

    async def _handle_create_subtask_tool(self, *args: Any, **kwargs: Any) -> str:
        return await self._coord._handle_create_subtask_tool(*args, **kwargs)

    async def _run_quality_checks(self, *args: Any, **kwargs: Any) -> dict:
        return await self._coord._run_quality_checks(*args, **kwargs)

    @staticmethod
    def _validate_debugger_output(submit_data: dict | None) -> dict:
        from agents.orchestrator.coordinator import AgentCoordinator
        return AgentCoordinator._validate_debugger_output(submit_data)

    async def _append_progress_log(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._append_progress_log(*args, **kwargs)

    async def _persist_execution_events(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._persist_execution_events(*args, **kwargs)

    async def _is_cancelled(self) -> bool:
        return await self._coord._is_cancelled()

    async def _post_system_message(self, *args: Any, **kwargs: Any) -> None:
        await self._coord._post_system_message(*args, **kwargs)

    async def _load_todo(self) -> dict:
        return await self._coord._load_todo()

    async def _resolve_agent_config(self, *args: Any, **kwargs: Any) -> Any:
        return await self._coord._resolve_agent_config(*args, **kwargs)

    @staticmethod
    def _get_builtin_tools(workspace_path: str, role: str = "coder") -> list[dict]:
        return get_builtin_tool_schemas(workspace_path, role)

    def _get_dep_index_dirs(self, workspace_path: str) -> dict[str, str]:
        return self._coord._get_dep_index_dirs(workspace_path)

    # ------------------------------------------------------------------
    # Single-shot execution
    # ------------------------------------------------------------------

    async def execute_single(
        self,
        sub_task: dict,
        provider: AIProvider,
        *,
        workspace_path: str | None = None,
    ) -> None:
        """Execute a single sub-task using the appropriate specialist agent."""
        st_id = str(sub_task["id"])
        logger.info("[%s] execute_single START: st=%s role=%s title=%s workspace=%s",
                    self.todo_id, st_id, sub_task["agent_role"], sub_task["title"], workspace_path)
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        # Update parent TODO sub_state
        await self.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            sub_task["agent_role"],
        )

        # Create agent run record
        run = await self.db.fetchrow(
            """
            INSERT INTO agent_runs (
                todo_id, sub_task_id, agent_role, agent_model, provider_type
            )
            VALUES ($1, $2, $3, $4, $5) RETURNING *
            """,
            self.todo_id,
            sub_task["id"],
            sub_task["agent_role"],
            provider.default_model,
            provider.provider_type,
        )

        start_time = time.monotonic()

        # Build context for the agent
        previous_results = await self._coord._ctx.get_completed_results()
        todo = await self._load_todo()

        # Resolve custom agent config
        agent_config = await self._resolve_agent_config(
            sub_task["agent_role"], str(todo["creator_id"]),
        )

        agent_prompt = await self._coord._ctx.build_agent_prompt(
            sub_task, todo, previous_results,
            workspace_path=workspace_path, agent_config=agent_config,
        )

        # Resolve MCP tools and skills for this project
        mcp_tools = await self.tools_registry.resolve_tools(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )
        skills_context = await self.tools_registry.build_skills_context(
            project_id=str(todo["project_id"]),
            user_id=str(todo["creator_id"]),
        )

        # Filter MCP tools to only those allowed for this agent role.
        # Custom configs with tools_enabled take precedence; otherwise
        # fall back to the role's default tool list from the registry.
        if agent_config and agent_config.get("tools_enabled"):
            allowed = set(agent_config["tools_enabled"])
        else:
            allowed = set(get_default_tools(sub_task["agent_role"]))
        if mcp_tools:
            mcp_tools = [t for t in mcp_tools if t.get("name") in allowed]

        # Ensure agents always have workspace tools (built-in fallback)
        if workspace_path:
            builtin_tools = self._get_builtin_tools(workspace_path, sub_task["agent_role"])
            # Inject task-local index directory for semantic_search
            _exec_idx = os.path.join(workspace_path, ".agent_index")
            _exec_dep_dirs = self._get_dep_index_dirs(workspace_path)
            for _bt in builtin_tools:
                if _bt["name"] == "semantic_search":
                    _bt["_index_dir"] = _exec_idx
                    if _exec_dep_dirs:
                        _bt["_dep_index_dirs"] = _exec_dep_dirs
            if mcp_tools:
                existing_names = {t["name"] for t in mcp_tools}
                mcp_tools.extend(t for t in builtin_tools if t["name"] not in existing_names)
            else:
                mcp_tools = builtin_tools

        # Append skills context to system prompt
        system_prompt = agent_prompt["system"]
        if skills_context:
            system_prompt += skills_context

        # Model preference from custom agent
        model_override = agent_config.get("model_preference") if agent_config else None

        try:
            # Report progress
            await self._report_progress(st_id, 10, f"Starting: {sub_task['title']}")

            messages = [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=agent_prompt["user"]),
            ]
            tools_arg = mcp_tools if mcp_tools else None

            from agents.providers.base import run_tool_loop
            from agents.orchestrator.structured_output import build_submit_tool_for_role

            logger.info("[%s] st=%s Sending LLM request (tools=%d, model_override=%s)",
                        self.todo_id, st_id, len(tools_arg) if tools_arg else 0, model_override)

            # Roles that write code need more output tokens for write_file calls
            role = sub_task["agent_role"]
            role_max_tokens = 16384 if role in ("coder", "tester") else 8192

            # Structured output via submit_result tool
            _submit_tool = build_submit_tool_for_role(role)
            _submit_result_data: dict | None = None
            if _submit_tool and tools_arg:
                tools_arg = [t for t in tools_arg if t["name"] != "task_complete"]
                tools_arg.append(_submit_tool)

            send_kwargs: dict = {"temperature": 0.1, "max_tokens": role_max_tokens}
            if model_override:
                send_kwargs["model"] = model_override

            async def _on_tool_round(round_num: int, resp: LLMResponse) -> None:
                await self._report_progress(
                    st_id, 10 + round_num * 6,
                    f"Using tool: {resp.tool_calls[0].get('name', '?') if resp.tool_calls else '?'}",
                )

            async def _tool_exec(name: str, args: dict) -> str:
                nonlocal _submit_result_data
                if name == "submit_result":
                    _submit_result_data = args
                    return "Structured output received. Task complete."
                if name == "create_subtask":
                    return await self._handle_create_subtask_tool(
                        sub_task, args, workspace_path,
                    )
                return await self.mcp_executor.execute_tool(name, args, mcp_tools)

            # Structured tool event streaming (same pattern as iterative path)
            async def _on_tool_event(event: dict) -> None:
                try:
                    event["sub_task_id"] = st_id
                    event["ts"] = time.time()
                    await self.redis.publish(
                        f"task:{self.todo_id}:events",
                        json.dumps(event),
                    )
                except Exception:
                    logger.debug("Failed to publish tool event", exc_info=True)

            # Allow users to inject guidance into this subtask's tool loop
            _inject_key = f"subtask:{st_id}:inject"

            async def _check_inject() -> str | None:
                if self.redis:
                    return await self.redis.lpop(_inject_key)
                return None

            _exec_token_cb = self._build_token_streamer(st_id)
            content, response = await run_tool_loop(
                provider, messages,
                tools=tools_arg,
                tool_executor=_tool_exec,
                max_rounds=70,
                on_tool_round=_on_tool_round,
                on_activity=lambda msg: self._report_activity(st_id, msg),
                on_tool_event=_on_tool_event,
                on_inject_check=_check_inject,
                on_cancel_check=self._is_cancelled,
                on_token=_exec_token_cb,
                **send_kwargs,
            )
            if hasattr(_exec_token_cb, "flush"):
                await _exec_token_cb.flush()
            logger.info("[%s] st=%s Tool loop done: content_len=%d stop=%s",
                        self.todo_id, st_id, len(content) if content else 0, response.stop_reason)

            # Drain any unconsumed inject messages
            if self.redis:
                while await self.redis.lpop(_inject_key):
                    pass

            await self._report_progress(st_id, 80, f"Processing output: {sub_task['title']}")

            # -- Structured output validation --
            from agents.orchestrator.output_validator import (
                validate_agent_output,
                validate_agent_output_dict,
            )

            validated_output = None
            raw_content = content or response.content or ""

            # If submit_result was called, validate the dict directly
            if _submit_result_data is not None:
                validated_output, val_errors = validate_agent_output_dict(
                    sub_task["agent_role"], dict(_submit_result_data), raw_content,
                )
                if validated_output is None:
                    logger.warning(
                        "submit_result validation failed for %s (%s): %s — using raw dict",
                        st_id, sub_task["agent_role"], val_errors,
                    )
                    # Use the raw submit_result data rather than burning tokens on retries
                    validated_output = dict(_submit_result_data)
                    validated_output.setdefault("content", raw_content)

            # Fallback: single text-based extraction attempt (no retry loop)
            if validated_output is None:
                validated_output, val_errors = validate_agent_output(
                    sub_task["agent_role"], response.content,
                )

            if validated_output is None:
                logger.warning(
                    "Subtask %s (%s) output validation failed: %s — using raw content",
                    st_id, sub_task["agent_role"], val_errors,
                )
                validated_output = {"content": response.content, "raw_content": response.content}

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Update agent run
            await self.db.execute(
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

            # Update sub-task
            await self._transition_subtask(
                st_id,
                "completed",
                progress_pct=100,
                progress_message="Done",
                output_result=validated_output,
            )

            # Create deliverable if the agent produced one
            await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)

            # Update token tracking on the TODO
            await self.db.execute(
                """
                UPDATE todo_items
                SET actual_tokens = actual_tokens + $2,
                    cost_usd = cost_usd + $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                self.todo_id,
                response.tokens_input + response.tokens_output,
                response.cost_usd,
            )

            await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")

        except Exception as e:
            import traceback as _tb

            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_type = _classify_error(e)
            error_detail = f"{type(e).__name__}: {e}"
            error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

            await self.db.execute(
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

    # ------------------------------------------------------------------
    # Iterative (RALPH) execution
    # ------------------------------------------------------------------

    async def execute_iterative(
        self,
        sub_task: dict,
        provider: AIProvider,
        *,
        workspace_path: str | None = None,
        work_rules: dict | None = None,
        max_iterations: int = 50,
    ) -> None:
        """RALPH-style iterative execution: fresh context per iteration,
        quality checks, stuck detection, hard cutoff."""
        st_id = str(sub_task["id"])
        logger.info("[%s] execute_iterative START: st=%s role=%s title=%s workspace=%s max_iter=%d",
                    self.todo_id, st_id, sub_task["agent_role"], sub_task["title"], workspace_path, max_iterations)
        await self._transition_subtask(st_id, "assigned")
        await self._transition_subtask(st_id, "running")

        await self.db.execute(
            "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
            self.todo_id,
            sub_task["agent_role"],
        )

        iteration_log: list[dict] = []
        _accumulated_events: list[dict] = []  # Collect tool events for persistence
        agent_signaled_done = False
        agent_done_summary: str | None = None
        qc_retries_after_done = 0
        _MAX_QC_RETRIES_AFTER_DONE = 2  # Max extra iterations when agent signals done but QC fails
        role = sub_task["agent_role"]
        role_rules = self._coord._ctx.filter_rules_for_role(work_rules or {}, role)
        has_quality_rules = bool(role_rules.get("quality"))

        # Resolve custom agent config for this role
        todo_for_agent = await self._load_todo()
        agent_config = await self._resolve_agent_config(role, str(todo_for_agent["creator_id"]))
        # Resolution: agent config > task-level > provider default
        model_override = None
        if agent_config and agent_config.get("model_preference"):
            model_override = agent_config["model_preference"]
        elif todo_for_agent.get("ai_model"):
            model_override = todo_for_agent["ai_model"]

        # Load architect/editor config for this project
        _ae_project = await self.db.fetchrow(
            "SELECT architect_editor_enabled, architect_model, editor_model FROM projects WHERE id = $1",
            todo_for_agent["project_id"],
        )
        architect_editor_enabled = bool(_ae_project and _ae_project.get("architect_editor_enabled"))
        architect_model = (_ae_project or {}).get("architect_model")
        editor_model = (_ae_project or {}).get("editor_model")

        # -- Pre-resolve tools ONCE (avoid repeated DB queries per iteration) --
        _project_id = str(todo_for_agent["project_id"])
        _creator_id = str(todo_for_agent["creator_id"])
        _cached_mcp_tools = await self.tools_registry.resolve_tools(
            project_id=_project_id, user_id=_creator_id,
        )
        _cached_skills_ctx = await self.tools_registry.build_skills_context(
            project_id=_project_id, user_id=_creator_id,
        )
        # Filter MCP tools to only those allowed for this agent role.
        # Custom configs with tools_enabled take precedence; otherwise
        # fall back to the role's default tool list from the registry.
        if agent_config and agent_config.get("tools_enabled"):
            _allowed = set(agent_config["tools_enabled"])
        else:
            _allowed = set(get_default_tools(role))
        if _cached_mcp_tools:
            _cached_mcp_tools = [t for t in _cached_mcp_tools if t.get("name") in _allowed]

        # Merge built-in workspace tools with MCP tools
        _cached_all_tools: list[dict] | None = None
        if workspace_path:
            builtin_tools = self._get_builtin_tools(workspace_path, role)
            _task_idx = os.path.join(workspace_path, ".agent_index")
            _task_dep_dirs = self._get_dep_index_dirs(workspace_path)
            for bt in builtin_tools:
                if bt["name"] == "semantic_search":
                    bt["_index_dir"] = _task_idx
                    if _task_dep_dirs:
                        bt["_dep_index_dirs"] = _task_dep_dirs
            if _cached_mcp_tools:
                existing_names = {t["name"] for t in _cached_mcp_tools}
                _cached_all_tools = list(_cached_mcp_tools) + [
                    t for t in builtin_tools if t["name"] not in existing_names
                ]
            else:
                _cached_all_tools = builtin_tools
        elif _cached_mcp_tools:
            _cached_all_tools = _cached_mcp_tools

        # Cache repo map text across iterations (generated once on first iteration)
        _cached_repo_map: str | None = None

        for iteration in range(1, max_iterations + 1):
            # Check for cancellation at the start of each iteration
            if await self._is_cancelled():
                logger.info("[%s] Task cancelled during iteration %d of %s, aborting",
                            self.todo_id, iteration, st_id)
                return

            start_time = time.monotonic()

            # Emit iteration_start event for streaming visibility
            try:
                _iter_start_evt = {
                    "type": "iteration_start",
                    "sub_task_id": st_id,
                    "iteration": iteration,
                    "subtask": sub_task["title"],
                    "ts": time.time(),
                }
                await self.redis.publish(
                    f"task:{self.todo_id}:events",
                    json.dumps(_iter_start_evt),
                )
                if len(_accumulated_events) < 2000:
                    _accumulated_events.append(_iter_start_evt)
            except Exception:
                pass

            # Create agent run record
            run = await self.db.fetchrow(
                """
                INSERT INTO agent_runs (
                    todo_id, sub_task_id, agent_role, agent_model, provider_type
                )
                VALUES ($1, $2, $3, $4, $5) RETURNING *
                """,
                self.todo_id,
                sub_task["id"],
                role,
                provider.default_model,
                provider.provider_type,
            )

            try:
                await self._report_progress(
                    st_id,
                    min(10 + iteration * 2, 80),
                    f"Iteration {iteration}: {sub_task['title']}",
                )

                # 1. Build FRESH context (no carried conversation)
                context, _cached_repo_map = await self._coord._ctx.build_iteration_context(
                    sub_task=sub_task,
                    iteration=iteration,
                    iteration_log=iteration_log,
                    workspace_path=workspace_path,
                    work_rules=role_rules,
                    agent_config=agent_config,
                    cached_repo_map=_cached_repo_map,
                )

                # Use pre-resolved tools (cached before the loop)
                mcp_tools = _cached_all_tools

                system_prompt = context["system"]
                if _cached_skills_ctx:
                    system_prompt += _cached_skills_ctx

                # 2. Execute single iteration (one LLM call + tool loop)
                messages = [
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=context["user"]),
                ]
                tools_arg = mcp_tools if mcp_tools else None

                # Add submit_result tool for roles with output schemas
                from agents.orchestrator.structured_output import (
                    build_submit_tool_for_role, extract_submit_result as _extract_submit,
                )
                from agents.providers.base import run_tool_loop

                _submit_tool = build_submit_tool_for_role(role)
                _submit_result_data: dict | None = None
                if _submit_tool and tools_arg:
                    # Replace task_complete with submit_result for structured output roles
                    tools_arg = [t for t in tools_arg if t["name"] != "task_complete"]
                    tools_arg.append(_submit_tool)

                send_kwargs: dict = {"temperature": 0.1, "max_tokens": 16384}
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
                        return await self._handle_create_subtask_tool(
                            sub_task, args, workspace_path,
                        )
                    return await self.mcp_executor.execute_tool(name, args, mcp_tools)

                async def _on_tool_event(event: dict) -> None:
                    """Publish structured tool events for streaming execution visibility."""
                    try:
                        event["sub_task_id"] = st_id
                        event["ts"] = time.time()
                        await self.redis.publish(
                            f"task:{self.todo_id}:events",
                            json.dumps(event),
                        )
                        # Accumulate for DB persistence (cap at 2000 events)
                        if len(_accumulated_events) < 2000:
                            _accumulated_events.append(event)
                        # Publish extra index_search event when semantic_search completes
                        if event.get("type") == "tool_result" and event.get("name") == "semantic_search":
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
                                    await self.redis.publish(
                                        f"task:{self.todo_id}:events",
                                        json.dumps(idx_event),
                                    )
                                    if len(_accumulated_events) < 2000:
                                        _accumulated_events.append(idx_event)
                            except Exception:
                                pass
                    except Exception:
                        pass  # Don't let event publishing break execution

                # Allow users to inject guidance into this subtask's tool loop
                _inject_key_iter = f"subtask:{st_id}:inject"

                async def _check_inject_iter() -> str | None:
                    if self.redis:
                        return await self.redis.lpop(_inject_key_iter)
                    return None

                # Architect/Editor dual-model execution
                if architect_editor_enabled and architect_model and editor_model:
                    READ_ONLY_TOOLS = {"read_file", "list_directory", "search_files", "semantic_search"}
                    WRITE_TOOLS = {"write_file", "edit_file", "run_command", "task_complete", "create_subtask"}

                    # Phase A: Architect — powerful model with read-only tools
                    architect_tools = [t for t in (tools_arg or []) if t["name"] in READ_ONLY_TOOLS]
                    architect_kwargs = {**send_kwargs, "model": architect_model}

                    async def _architect_tool_exec(name: str, args: dict) -> str:
                        return await self.mcp_executor.execute_tool(name, args, mcp_tools)

                    _arch_token_cb = self._build_token_streamer(st_id)
                    architect_content, architect_response = await run_tool_loop(
                        provider, list(messages),
                        tools=architect_tools or None,
                        tool_executor=_architect_tool_exec,
                        max_rounds=70,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] [Architect] {msg}"),
                        on_tool_event=_on_tool_event,
                        on_inject_check=_check_inject_iter,
                        on_cancel_check=self._is_cancelled,
                        on_token=_arch_token_cb,
                        **architect_kwargs,
                    )
                    if hasattr(_arch_token_cb, "flush"):
                        await _arch_token_cb.flush()

                    # Phase B: Editor — fast model with write tools, guided by architect's output
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

                    _edit_token_cb = self._build_token_streamer(st_id)
                    iter_content, response = await run_tool_loop(
                        provider, editor_messages,
                        tools=editor_tools or None,
                        tool_executor=_iter_tool_exec,
                        max_rounds=70,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] [Editor] {msg}"),
                        on_tool_event=_on_tool_event,
                        on_inject_check=_check_inject_iter,
                        on_cancel_check=self._is_cancelled,
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
                    _std_token_cb = self._build_token_streamer(st_id)
                    iter_content, response = await run_tool_loop(
                        provider, messages,
                        tools=tools_arg,
                        tool_executor=_iter_tool_exec,
                        max_rounds=70,
                        on_activity=lambda msg, _i=iteration: self._report_activity(st_id, f"[iter {_i}] {msg}"),
                        on_tool_event=_on_tool_event,
                        on_inject_check=_check_inject_iter,
                        on_cancel_check=self._is_cancelled,
                        on_token=_std_token_cb,
                        **send_kwargs,
                    )
                    if hasattr(_std_token_cb, "flush"):
                        await _std_token_cb.flush()

                # Detect tool loop truncation — LLM wanted more tool calls but max_rounds hit
                tool_loop_truncated = response.stop_reason == "max_tool_rounds"
                if tool_loop_truncated:
                    logger.warning(
                        "[%s] Tool loop truncated at max_rounds for subtask %s iteration %d",
                        self.todo_id, st_id, iteration,
                    )

                # Broadcast the LLM's text response so the UI can display it
                if response.content and response.content.strip():
                    # Truncate for the activity stream (full content stored in iteration log)
                    preview = response.content.strip()
                    if len(preview) > 500:
                        preview = preview[:500] + "..."
                    await self.redis.publish(
                        f"task:{self.todo_id}:progress",
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
                await self.db.execute(
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

                # Track tokens on TODO
                await self._track_tokens(response)

                # 3. Run quality checks (if quality rules exist)
                if has_quality_rules and workspace_path:
                    qc_result = await self._run_quality_checks(
                        workspace_path, role_rules, role,
                        submit_data=_submit_result_data,
                    )
                elif role == "debugger" and agent_signaled_done and _submit_result_data:
                    qc_result = self._validate_debugger_output(_submit_result_data)
                else:
                    qc_result = {"passed": True, "reason": "no quality rules", "learnings": []}

                # Report QC result as activity
                if has_quality_rules and workspace_path:
                    if qc_result["passed"]:
                        await self._report_activity(st_id, f"[iter {iteration}] Quality check passed")
                    else:
                        reason = qc_result.get("reason", "failed")
                        await self._report_activity(st_id, f"[iter {iteration}] Quality check failed: {reason}")

                # 4. Record iteration
                action = "implement" if iteration == 1 else "fix"
                learnings = qc_result.get("learnings", [])
                if tool_loop_truncated:
                    learnings.append(
                        "Tool loop was truncated (hit max_rounds=70). "
                        "Agent may not have finished all intended tool calls."
                    )
                entry = {
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
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                    sub_task["id"],
                    iteration_log,
                )

                # Emit iteration_end event for streaming visibility
                try:
                    _iter_end_evt = {
                        "type": "iteration_end",
                        "sub_task_id": st_id,
                        "iteration": iteration,
                        "status": "passed" if qc_result["passed"] else qc_result.get("reason", "failed"),
                        "ts": time.time(),
                    }
                    await self.redis.publish(
                        f"task:{self.todo_id}:events",
                        json.dumps(_iter_end_evt),
                    )
                    if len(_accumulated_events) < 2000:
                        _accumulated_events.append(_iter_end_evt)
                except Exception:
                    pass

                # 5. Quality checks passed -> validate output then DONE
                if qc_result["passed"]:
                    from agents.orchestrator.output_validator import (
                        validate_agent_output as _validate_output,
                        build_correction_prompt as _build_correction,
                        MAX_VALIDATION_RETRIES as _MAX_VAL_RETRIES,
                    )
                    validated_output = None
                    for val_attempt in range(_MAX_VAL_RETRIES + 1):
                        validated_output, val_errors = _validate_output(
                            role, response.content,
                        )
                        if validated_output is not None:
                            break
                        if val_attempt < _MAX_VAL_RETRIES:
                            correction = _build_correction(role, val_errors, response.content)
                            messages.append(LLMMessage(role="assistant", content=response.content))
                            messages.append(LLMMessage(role="user", content=correction))
                            response = await provider.send_message(
                                messages, temperature=0.1, tools=tools_arg,
                                **({"model": model_override} if model_override else {}),
                            )

                    if validated_output is None:
                        logger.warning(
                            "RALPH subtask %s (%s) failed output validation after %d retries: %s",
                            st_id, role, _MAX_VAL_RETRIES, val_errors,
                        )
                        validated_output = {
                            "content": response.content,
                            "raw_content": response.content,
                            "_validation_failed": True,
                            "_validation_errors": val_errors,
                        }

                    await self._transition_subtask(
                        st_id, "completed",
                        progress_pct=100,
                        progress_message="Done",
                        output_result=validated_output,
                    )
                    await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)
                    await self._append_progress_log(
                        sub_task, iteration, "completed", iteration_log,
                    )
                    await self._persist_execution_events(st_id, _accumulated_events)
                    await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")
                    return

                # 5b. Agent signaled done via task_complete
                if agent_signaled_done:
                    # If quality checks failed, don't accept — force the agent to fix
                    if not qc_result["passed"] and qc_retries_after_done < _MAX_QC_RETRIES_AFTER_DONE:
                        qc_retries_after_done += 1
                        logger.warning(
                            "[%s] Agent signaled task_complete on iteration %d but QC failed "
                            "(retry %d/%d): %s",
                            self.todo_id, iteration, qc_retries_after_done,
                            _MAX_QC_RETRIES_AFTER_DONE, qc_result.get("reason", "unknown"),
                        )
                        # Record the premature completion attempt in iteration log
                        entry["outcome"] = f"agent_done_but_qc_failed ({qc_result.get('reason', 'unknown')})"
                        entry["error_output"] = qc_result.get("error_output")
                        entry["learnings"].append(
                            "Agent signaled task_complete but quality checks did not pass. "
                            "Continuing iteration to fix quality issues."
                        )
                        iteration_log[-1] = entry
                        await self.db.execute(
                            "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                            sub_task["id"], iteration_log,
                        )
                        # Reset the signal so the loop continues
                        agent_signaled_done = False
                        agent_done_summary = ""
                        # Skip to next iteration — context builder will include QC failure
                        continue

                    # QC passed (or retries exhausted) — accept the result
                    if not qc_result["passed"]:
                        logger.warning(
                            "[%s] Agent signaled task_complete, QC still failing after %d retries — "
                            "accepting with warning",
                            self.todo_id, _MAX_QC_RETRIES_AFTER_DONE,
                        )
                    logger.info(
                        "[%s] Agent signaled task_complete on iteration %d (qc=%s)",
                        self.todo_id, iteration, "passed" if qc_result["passed"] else "failed_exhausted",
                    )

                    from agents.orchestrator.output_validator import (
                        validate_agent_output as _validate_output_done,
                    )
                    validated_output, _ = _validate_output_done(role, response.content)
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

                    await self._transition_subtask(
                        st_id, "completed",
                        progress_pct=100,
                        progress_message=agent_done_summary or "Done",
                        output_result=validated_output,
                    )
                    await self._maybe_create_deliverable(sub_task, response, run, workspace_path=workspace_path)
                    await self._append_progress_log(
                        sub_task, iteration, "completed_agent_signal", iteration_log,
                    )
                    await self._persist_execution_events(st_id, _accumulated_events)
                    await self._report_progress(st_id, 100, f"Completed: {sub_task['title']}")
                    return


            except Exception as e:
                import traceback as _tb

                duration_ms = int((time.monotonic() - start_time) * 1000)
                error_type = _classify_error(e)
                error_detail = f"{type(e).__name__}: {e}"
                error_traceback = "".join(_tb.format_exception(type(e), e, e.__traceback__))

                await self.db.execute(
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
                # Record failure in iteration log with full context
                iteration_log.append({
                    "iteration": iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "error",
                    "outcome": f"exception: {error_detail[:500]}",
                    "error_output": error_traceback[-3000:],
                    "learnings": [],
                    "tokens_used": 0,
                })
                await self.db.execute(
                    "UPDATE sub_tasks SET iteration_log = $2 WHERE id = $1",
                    sub_task["id"],
                    iteration_log,
                )
                # Report the error as activity so it appears in the UI immediately
                await self.redis.publish(
                    f"task:{self.todo_id}:progress",
                    json.dumps({
                        "type": "activity",
                        "sub_task_id": st_id,
                        "activity": f"ERROR: {error_detail[:300]}",
                    }),
                )
                raise

        # 7. Hard cutoff — max_iterations reached
        await self._post_system_message(
            f"**Sub-task failed after {max_iterations} iterations:** {sub_task['title']}"
        )
        await self._append_progress_log(
            sub_task, max_iterations, "failed_max_iterations", iteration_log,
        )
        await self._persist_execution_events(st_id, _accumulated_events)
        await self._transition_subtask(
            st_id, "failed",
            error_message=f"Max iterations ({max_iterations}) reached without passing quality checks",
        )
