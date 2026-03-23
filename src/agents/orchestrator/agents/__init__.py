"""Agent registry — maps role strings to agent classes."""

from __future__ import annotations

from agents.orchestrator.agents._base import BaseAgent
from agents.orchestrator.agents.build_watcher import BuildWatcherAgent
from agents.orchestrator.agents.coder import CoderAgent
from agents.orchestrator.agents.debugger import DebuggerAgent
from agents.orchestrator.agents.deployer import DeployerAgent
from agents.orchestrator.agents.merge import MergeAgent
from agents.orchestrator.agents.merge_observer import MergeObserverAgent
from agents.orchestrator.agents.pr_creator import PrCreatorAgent
from agents.orchestrator.agents.reviewer import ReviewerAgent
from agents.orchestrator.agents.tester import TesterAgent

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "coder": CoderAgent,
    "reviewer": ReviewerAgent,
    "tester": TesterAgent,
    "debugger": DebuggerAgent,
    "pr_creator": PrCreatorAgent,
    "merge_agent": MergeAgent,
    "merge_observer": MergeObserverAgent,
    "release_build_watcher": BuildWatcherAgent,
    "release_deployer": DeployerAgent,
}

# Roles that are LLM-powered (use job_runner.run_llm_job)
LLM_ROLES = {"coder", "reviewer", "tester", "debugger"}

# Roles that are procedural (use job_runner.run_procedural_job)
PROCEDURAL_ROLES = {"pr_creator", "merge_agent", "merge_observer", "release_build_watcher", "release_deployer"}


def get_agent(role: str) -> BaseAgent:
    """Instantiate an agent by role name."""
    cls = AGENT_REGISTRY.get(role)
    if cls is None:
        raise ValueError(f"Unknown agent role: {role!r}. Known: {list(AGENT_REGISTRY.keys())}")
    return cls()


def is_llm_role(role: str) -> bool:
    """Check if a role uses LLM execution (as opposed to procedural)."""
    return role in LLM_ROLES
