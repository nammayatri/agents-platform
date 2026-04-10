"""Shared error classification utilities.

Single canonical source for error classification and agent output validation
used across the orchestrator (job_runner, agent_executor, etc.).
"""

from __future__ import annotations


def classify_error(exc: Exception) -> str:
    """Classify an exception into a category for the ``error_type`` field.

    Categories: timeout, rate_limit, auth_error, context_length,
    network, parse_error, transient.
    """
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


def validate_debugger_output(submit_data: dict | None) -> dict:
    """Validate that a debugger produced substantive findings.

    Returns ``{"passed": bool, "reason": str, ...}``.
    """
    if not submit_data:
        return {"passed": True, "reason": "no structured output", "learnings": []}

    root_cause = (submit_data.get("root_cause") or "").strip()
    evidence = submit_data.get("evidence") or []

    if len(root_cause) < 20:
        return {
            "passed": False,
            "reason": "Root cause too vague — investigate further with specific file paths and evidence.",
            "error_output": "Root cause must describe the specific code/config issue.",
            "learnings": ["Provide a detailed root cause referencing file paths and line numbers"],
        }

    if not evidence:
        return {
            "passed": False,
            "reason": "No evidence collected. Use tools to gather log lines, code paths, or query results.",
            "error_output": "Evidence list is empty.",
            "learnings": ["Collect at least one piece of evidence before concluding"],
        }

    return {"passed": True, "reason": "debugger output valid", "learnings": []}
