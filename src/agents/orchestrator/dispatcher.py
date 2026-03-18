"""Subtask dispatch routing — maps agent roles to execution methods.

Eliminates the repeated if/elif dispatch blocks in the coordinator's
_phase_execution method. The coordinator has 3 separate locations where
it routes subtasks by agent_role; this class centralises that logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Type alias for the handler callables.
# Role-specific handlers: (sub_task, provider, workspace_path)
# Generic handlers:       (sub_task, provider, *, workspace_path, ...)
_Handler = Callable[..., Awaitable[None]]


class SubtaskDispatcher:
    """Routes subtask execution to the correct method based on agent_role.

    Role-specific handlers (merge_agent, pr_creator, ...) are called with
    positional ``(sub_task, provider, workspace_path)``.

    When no role-specific route matches, the dispatcher falls back to
    either the *iterative* or *simple* handler depending on ``use_iterations``.
    """

    def __init__(
        self,
        *,
        execute_simple: _Handler,
        execute_iterative: _Handler,
        role_handlers: dict[str, _Handler] | None = None,
    ) -> None:
        self._execute_simple = execute_simple
        self._execute_iterative = execute_iterative
        self._role_routes: dict[str, _Handler] = dict(role_handlers or {})

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def register(self, role: str, handler: _Handler) -> None:
        """Register (or replace) a role-specific handler at runtime."""
        self._role_routes[role] = handler

    def build_coro(
        self,
        sub_task: dict,
        provider: Any,
        workspace_path: str | None,
        *,
        use_iterations: bool = False,
        work_rules: dict | None = None,
        max_iterations: int = 50,
    ) -> Awaitable[None]:
        """Return an *unawaited* coroutine for executing ``sub_task``.

        Callers typically collect these into a list and pass to
        ``asyncio.gather``.
        """
        agent_role = sub_task.get("agent_role", "")

        if agent_role in self._role_routes:
            # Role-specific handlers use positional workspace_path
            return self._role_routes[agent_role](sub_task, provider, workspace_path)

        if use_iterations:
            return self._execute_iterative(
                sub_task,
                provider,
                workspace_path=workspace_path,
                work_rules=work_rules,
                max_iterations=max_iterations,
            )

        return self._execute_simple(sub_task, provider, workspace_path=workspace_path)

    async def dispatch(
        self,
        sub_task: dict,
        provider: Any,
        workspace_path: str | None,
        *,
        use_iterations: bool = False,
        work_rules: dict | None = None,
        max_iterations: int = 50,
    ) -> None:
        """Await a single subtask dispatch (convenience wrapper)."""
        await self.build_coro(
            sub_task,
            provider,
            workspace_path,
            use_iterations=use_iterations,
            work_rules=work_rules,
            max_iterations=max_iterations,
        )

    async def dispatch_batch(
        self,
        subtasks: list[dict],
        provider: Any,
        workspace_map: dict[str, str | None],
        *,
        use_iterations: bool = False,
        work_rules: dict | None = None,
        max_iterations: int = 50,
    ) -> list[BaseException | None]:
        """Dispatch multiple subtasks concurrently via ``asyncio.gather``.

        Args:
            subtasks: List of subtask dicts to execute.
            provider: AI provider instance.
            workspace_map: Mapping of ``str(subtask["id"])`` → workspace path.
            use_iterations: Whether to use iterative execution.
            work_rules: Quality rules dict forwarded to iterative handler.
            max_iterations: Cap forwarded to iterative handler.

        Returns:
            List aligned with *subtasks*: ``None`` on success, the
            ``Exception`` instance on failure.
        """
        coros = [
            self.build_coro(
                st,
                provider,
                workspace_map.get(str(st.get("id", "")), None),
                use_iterations=use_iterations,
                work_rules=work_rules,
                max_iterations=max_iterations,
            )
            for st in subtasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        return list(results)
