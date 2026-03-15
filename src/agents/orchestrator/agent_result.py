"""Core result types for the job-based orchestrator.

AgentResult: what an agent returns after running a job.
JobSpec: declarative specification for a follow-up job to create.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JobSpec:
    """Declarative specification for a follow-up job.

    Agents return these in AgentResult.spawn to tell the scheduler
    what jobs to create next — no handler registries needed.
    """

    title: str
    description: str
    role: str
    depends_on_parent: bool = True
    """Auto-depend on the job that spawned this."""
    depends_on_siblings: bool = False
    """Depend on ALL other spawned jobs in the same batch.
    Used for re-reviewer/re-tester that waits for all fix coders."""
    chain_id: str | None = None
    """Review chain tracking. Passed through from parent to children
    so the scheduler can enforce round caps."""
    target_repo: dict | None = None
    """For dependency repo work. Copied from parent job if not set."""
    review_loop: bool = False
    """Whether this job participates in the review loop."""


@dataclass
class AgentResult:
    """What an agent returns after running a job.

    The scheduler uses `spawn` to create follow-up jobs — this is
    the key mechanism that replaces handler registries.
    """

    output: dict = field(default_factory=dict)
    """Structured result stored in sub_tasks.output_result."""
    spawn: list[JobSpec] = field(default_factory=list)
    """Follow-up jobs to create. The scheduler handles dependency wiring."""
    artifacts: list[dict] = field(default_factory=list)
    """Deliverables (PRs, documents, etc.) to record."""
