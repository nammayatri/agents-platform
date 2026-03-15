"""Shared utilities for resolving target_repo references against project context_docs."""

import logging

logger = logging.getLogger(__name__)


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
