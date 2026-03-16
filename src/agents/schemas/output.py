"""Pydantic schemas for structured agent output validation.

Each agent role defines the fields it MUST produce. The ``raw_content`` field
preserves the full LLM response text for deliverables / display. The structured
fields are what downstream logic (review loops, merge decisions, reports) relies on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Base ────────────────────────────────────────────────────────────


class BaseAgentOutput(BaseModel):
    """Every agent output carries the full LLM text (auto-injected by the
    validator, *not* produced by the agent itself)."""

    raw_content: str = Field(
        default="",
        description="Full LLM response text, preserved for deliverables",
    )


# ── Coder ───────────────────────────────────────────────────────────


class CoderOutput(BaseAgentOutput):
    approach: str = Field(description="Implementation approach taken")
    files_changed: list[str] = Field(
        default_factory=list,
        description="File paths created or modified",
    )
    setup_steps: list[str] = Field(
        default_factory=list,
        description="Setup/migration/config steps needed",
    )


# ── Tester ──────────────────────────────────────────────────────────


class TestFailure(BaseModel):
    file: str = Field(default="", description="File path where the failure occurred")
    type: Literal["build", "type_error", "test", "lint", "runtime"] = Field(
        description="Failure category",
    )
    error: str = Field(description="Error message or output")


class TesterOutput(BaseAgentOutput):
    passed: bool = Field(description="Whether all tests/checks passed")
    summary: str = Field(description="Brief summary of test results")
    test_files: list[str] = Field(
        default_factory=list,
        description="Test file paths created or run",
    )
    failures: list[TestFailure] = Field(
        default_factory=list,
        description="Structured list of failures found",
    )


# ── Reviewer ────────────────────────────────────────────────────────


class ReviewIssue(BaseModel):
    severity: Literal["critical", "major", "minor", "nit"] = Field(
        description="Issue severity",
    )
    file: str = Field(default="", description="File path where the issue is located")
    line: int | None = Field(default=None, description="Line number (approximate)")
    description: str = Field(description="What the issue is")
    suggestion: str = Field(default="", description="How to fix it")


class ReviewerOutput(BaseAgentOutput):
    verdict: Literal["approved", "needs_changes"] = Field(
        description="Review verdict",
    )
    approved: bool = Field(description="True if code is acceptable")
    matches_plan: bool = Field(
        default=True,
        description="Implementation matches planned approach",
    )
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(description="Brief overall assessment")
    needs_human_review: bool = Field(
        default=False,
        description="True only when uncertain",
    )


# ── PR Creator ──────────────────────────────────────────────────────


class PRCreatorOutput(BaseAgentOutput):
    pr_title: str = Field(description="PR title")
    pr_body: str = Field(description="PR description in markdown")
    breaking_changes: list[str] = Field(default_factory=list)


# ── Report Writer ───────────────────────────────────────────────────


class ReportWriterOutput(BaseAgentOutput):
    title: str = Field(description="Report title")
    executive_summary: str = Field(description="1-3 sentence summary")
    report_body: str = Field(description="Full report in markdown")


# ── Merge Agent ─────────────────────────────────────────────────────


class MergeAgentOutput(BaseAgentOutput):
    merge_decision: Literal["merge", "block", "skip"] = Field(
        description="Merge decision",
    )
    reason: str = Field(description="Why this decision was made")
    ci_passed: bool = Field(default=False)


# ── Debugger ───────────────────────────────────────────────────────


class DebuggerOutput(BaseAgentOutput):
    root_cause: str = Field(description="Identified root cause of the issue")
    evidence: list[str] = Field(
        default_factory=list,
        description="Log entries, query results, code paths supporting the diagnosis",
    )
    fix_applied: bool = Field(
        default=False,
        description="Whether a fix was applied in this run",
    )
    files_changed: list[str] = Field(
        default_factory=list,
        description="Files modified (if fix was applied)",
    )
    recommendation: str = Field(
        default="",
        description="Next steps or recommendation",
    )


# ── Registry ────────────────────────────────────────────────────────

ROLE_OUTPUT_SCHEMAS: dict[str, type[BaseAgentOutput]] = {
    "coder": CoderOutput,
    "tester": TesterOutput,
    "reviewer": ReviewerOutput,
    "pr_creator": PRCreatorOutput,
    "report_writer": ReportWriterOutput,
    "merge_agent": MergeAgentOutput,
    "debugger": DebuggerOutput,
}
