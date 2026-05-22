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


# Error types that are permanent — retrying will not help
_PERMANENT_ERRORS = {"auth_error", "context_length"}

# Error types that are transient — retrying may succeed
_TRANSIENT_ERRORS = {"timeout", "rate_limit", "network", "transient"}


def is_retryable_error(error_message: str | None, error_type: str | None = None) -> bool:
    """Determine if a failed subtask should be retried based on its error.

    Permanent errors (auth, invalid config, missing resources) should NOT be retried.
    Transient errors (timeout, rate limit, network) SHOULD be retried.
    """
    if error_type and error_type in _PERMANENT_ERRORS:
        return False
    if error_type and error_type in _TRANSIENT_ERRORS:
        return True

    # Heuristic: check error message for permanent failure indicators
    if not error_message:
        return True  # unknown error, retry by default
    msg = error_message.lower()
    permanent_indicators = [
        "precondition failed",
        "not authorized",
        "unauthorized",
        "authentication",
        "invalid config",
        "no repo_url",
        "no repo configured",
        "invalid target_repo",
        "not found",
        "permission denied",
    ]
    if any(ind in msg for ind in permanent_indicators):
        return False
    return True


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
