"""Shared utilities for resolving target_repo references against project context_docs.

Canonical helpers for target_repo parsing and repo name extraction.
All orchestrator code should use these instead of inline json.loads / isinstance checks.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Canonical target_repo parsing
# ──────────────────────────────────────────────────────────────────────

def parse_target_repo(raw) -> dict | None:
    """Parse a target_repo field into a dict (or None).

    Handles: None, "", dict, JSON string, plain string name.
    This is the ONE place that normalizes target_repo from the DB.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw if raw else None
    if isinstance(raw, str):
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            # Plain string name like "my-dep" — wrap it
            return {"name": raw} if raw.strip() else None
    return None


def repo_name_of(subtask_or_target_repo) -> str:
    """Extract the repo name from a subtask dict or a raw target_repo value.

    Returns "main" for the main repo, or the dependency name otherwise.
    This is the ONE canonical repo name resolver.
    """
    # Accept either a subtask row (has "target_repo" key) or a raw value
    if isinstance(subtask_or_target_repo, dict) and "id" in subtask_or_target_repo:
        raw = subtask_or_target_repo.get("target_repo")
    else:
        raw = subtask_or_target_repo

    parsed = parse_target_repo(raw)
    if not parsed:
        return "main"

    name = parsed.get("name", "")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "main"


def resolve_target_repo(
    value: str | dict | None, context_docs: list[dict],
) -> dict | None:
    """Resolve a target_repo name to full repo metadata from context_docs.

    Args:
        value: The raw target_repo value — "main", a dep name string, or a dict.
        context_docs: The project's configured dependency list.

    Returns:
        None for "main" or unresolved (use default workspace).
        A metadata dict with repo_url, name, default_branch, git_provider_id
        for a matched dependency.
    """
    if not value or not context_docs:
        return None

    # Already resolved — pass through
    if isinstance(value, dict) and value.get("repo_url"):
        return value

    # Normalize to string
    name = str(value.get("name", "") if isinstance(value, dict) else value).strip()
    if not name or name.lower() == "main":
        return None

    target = name.lower()

    for dep in context_docs:
        if not isinstance(dep, dict) or not dep.get("name"):
            continue
        dep_name = dep["name"]

        # Build all name variants for this dep
        candidates = {dep_name.lower()}
        if "/" in dep_name:
            candidates.add(dep_name.rsplit("/", 1)[-1].lower())
        candidates.add(dep_name.replace("/", "_").replace(" ", "_").lower())
        repo_url = dep.get("repo_url", "")
        if repo_url:
            candidates.add(
                repo_url.rstrip("/").rsplit("/", 1)[-1].replace(".git", "").lower()
            )

        # Exact or partial match
        if target in candidates or any(
            len(c) >= 3 and (target in c or c in target) for c in candidates
        ):
            return {
                "repo_url": repo_url,
                "name": dep_name,
                "default_branch": dep.get("default_branch", "main"),
                "git_provider_id": dep.get("git_provider_id"),
            }

    logger.warning(
        "Could not resolve target_repo '%s' against %d context_docs",
        name, len(context_docs),
    )
    return None
