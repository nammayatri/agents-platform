"""Base agent classes.

BaseAgent: ABC for all agents (LLM-powered and procedural).
LLMAgent: Base for agents that use LLM + tool loop.

Subclasses only implement:
  - build_prompt() — what context to give the LLM
  - decide_spawn() — what follow-up jobs to create
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agents.orchestrator.agent_result import AgentResult, JobSpec

if TYPE_CHECKING:
    from agents.orchestrator.run_context import RunContext

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base for all agents — LLM-powered and procedural."""

    role: str = ""

    @abstractmethod
    async def run(
        self,
        job: dict,
        workspace: str | None,
        ctx: RunContext,
    ) -> AgentResult:
        """Execute a job and return the result with spawn declarations.

        Parameters
        ----------
        job : dict
            The sub_tasks row (all columns).
        workspace : str | None
            Path to the task workspace (contains repo/, deps/, etc.).
        ctx : RunContext
            All infrastructure services.

        Returns
        -------
        AgentResult
            output + spawn declarations + artifacts.
        """


class LLMAgent(BaseAgent):
    """Base for agents that use LLM + tool loop.

    Subclasses must implement:
      - build_prompt(job, workspace, ctx, todo, **kw) -> {"system": str, "user": str}
      - decide_spawn(job, output) -> list[JobSpec]

    The LLM execution (single-shot or iterative RALPH) is handled by
    job_runner.py, which calls build_prompt() and decide_spawn() at the
    right times. This class just defines the interface.
    """

    @abstractmethod
    async def build_prompt(
        self,
        job: dict,
        workspace: str | None,
        ctx: RunContext,
        todo: dict,
        *,
        iteration: int = 0,
        iteration_log: list[dict] | None = None,
        work_rules: dict | None = None,
        agent_config: dict | None = None,
        cached_repo_map: str | None = None,
    ) -> dict:
        """Build the system + user prompt for this agent.

        Returns {"system": str, "user": str}.
        For iterative agents, iteration > 0 means rebuild with fresh context.
        """

    @abstractmethod
    def decide_spawn(self, job: dict, output: dict) -> list[JobSpec]:
        """Declare what follow-up jobs should be created after this job completes.

        Called by the scheduler after the job finishes successfully.
        Return [] if no follow-up is needed.
        """

    async def run(
        self,
        job: dict,
        workspace: str | None,
        ctx: RunContext,
    ) -> AgentResult:
        """Default implementation — overridden if agent needs custom flow.

        Most LLM agents don't override this. The job_runner calls
        build_prompt() and decide_spawn() directly. This is here
        for agents that need completely custom execution.
        """
        raise NotImplementedError(
            "LLMAgent.run() should not be called directly. "
            "Use job_runner.run_llm_job() instead."
        )
