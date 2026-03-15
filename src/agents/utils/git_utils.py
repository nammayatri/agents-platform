"""Shared git utility functions used across API routes and the orchestrator.

Consolidates duplicated git operations that previously existed in both
`api/routes/todos.py` and `orchestrator/workspace.py`.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def run_git_command(*args: str, cwd: str) -> tuple[int, str]:
    """Run a git command asynchronously with terminal prompts disabled.

    Returns (return_code, combined_stdout_stderr).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace")


async def resolve_git_credentials(
    db,
    git_provider_id: str | None,
    repo_url: str,
) -> tuple[str | None, str | None, str | None]:
    """Resolve git credentials (token, provider_type, api_base_url) from the database.

    If git_provider_id is set, looks up that specific provider config.
    Otherwise, auto-detects provider type from the repo URL and falls back
    to the first matching active provider config.

    Returns (token, provider_type, api_base_url).
    """
    from agents.infra.crypto import decrypt
    from agents.orchestrator.git_providers.factory import detect_provider_type

    token = None
    provider_type = None
    api_base_url = None

    if git_provider_id:
        row = await db.fetchrow(
            "SELECT provider_type, api_base_url, token_enc "
            "FROM git_provider_configs WHERE id = $1 AND is_active = TRUE",
            git_provider_id,
        )
        if row:
            token = decrypt(row["token_enc"]) if row.get("token_enc") else None
            provider_type = row["provider_type"]
            api_base_url = row.get("api_base_url")

    if not token and repo_url:
        detected = detect_provider_type(repo_url)
        if detected:
            row = await db.fetchrow(
                "SELECT provider_type, api_base_url, token_enc "
                "FROM git_provider_configs "
                "WHERE provider_type = $1 AND is_active = TRUE "
                "LIMIT 1",
                detected,
            )
            if row:
                token = decrypt(row["token_enc"]) if row.get("token_enc") else None
                provider_type = row["provider_type"]
                api_base_url = row.get("api_base_url")
                logger.info(
                    "Resolved git credentials via fallback: provider_type=%s",
                    provider_type,
                )

    if not provider_type:
        provider_type = detect_provider_type(repo_url)

    return token, provider_type, api_base_url


async def ensure_authenticated_remote(repo_dir: str, db, *, project_id: str | None = None) -> None:
    """Ensure the remote origin URL has credentials for push.

    Reads the current remote URL, resolves credentials from the database,
    and updates the remote URL to include the token.

    If project_id is provided, looks up the project's git_provider_id first.
    """
    from agents.orchestrator.git_providers.factory import build_clone_url

    rc, current_url = await run_git_command("remote", "get-url", "origin", cwd=repo_dir)
    if rc != 0 or not current_url.strip():
        return

    current_url = current_url.strip()

    # Skip if already authenticated
    if "@" in current_url and "x-access-token" in current_url:
        return

    git_provider_id = None
    if project_id:
        project = await db.fetchrow(
            "SELECT git_provider_id FROM projects WHERE id = $1", project_id,
        )
        if project and project.get("git_provider_id"):
            git_provider_id = str(project["git_provider_id"])

    token, provider_type, _ = await resolve_git_credentials(db, git_provider_id, current_url)
    if not token:
        logger.warning(
            "No configured git credentials found for %s — push may fail",
            current_url,
        )
        return

    authenticated_url = build_clone_url(current_url, token, provider_type)
    await run_git_command("remote", "set-url", "origin", authenticated_url, cwd=repo_dir)
    logger.info("Updated remote origin to use configured credentials")
