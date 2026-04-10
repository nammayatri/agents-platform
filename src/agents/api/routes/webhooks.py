"""Inbound webhook endpoints for GitHub and GitLab event delivery.

These endpoints do NOT require JWT authentication. Instead, they verify
webhook payloads using HMAC-SHA256 (GitHub) or secret token headers (GitLab).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()


# ------------------------------------------------------------------
# Signature / token verification
# ------------------------------------------------------------------

def _verify_github_signature(
    payload_body: bytes, secret: str, signature_header: str | None,
) -> bool:
    """Verify GitHub's X-Hub-Signature-256 HMAC."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_gitlab_token(secret: str, token_header: str | None) -> bool:
    """Verify GitLab's X-Gitlab-Token header."""
    if not token_header:
        return False
    return hmac.compare_digest(secret, token_header)


async def _get_webhook_secret(db, project_id: str, provider_type: str) -> str | None:
    """Load the webhook secret for a project + provider."""
    row = await db.fetchrow(
        "SELECT secret FROM webhook_secrets "
        "WHERE project_id = $1 AND provider_type = $2 AND is_active = TRUE",
        project_id, provider_type,
    )
    return row["secret"] if row else None


# ------------------------------------------------------------------
# GitHub webhook
# ------------------------------------------------------------------

@router.post("/webhooks/github/{project_id}")
async def github_webhook(project_id: str, request: Request):
    """Receive GitHub webhook events."""
    db = request.app.state.db
    redis = request.app.state.redis
    event_bus = request.app.state.event_bus

    body = await request.body()

    # Verify signature
    secret = await _get_webhook_secret(db, project_id, "github")
    if secret:
        sig = request.headers.get("X-Hub-Signature-256")
        if not _verify_github_signature(body, secret, sig):
            raise HTTPException(status_code=403, detail="Invalid signature")
    else:
        logger.warning(
            "No webhook secret for project %s (github), accepting unverified",
            project_id[:8],
        )

    event_type = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(
        "[webhook] github event=%s project=%s action=%s",
        event_type, project_id[:8], payload.get("action", ""),
    )

    if event_type == "pull_request":
        pr = payload.get("pull_request", {})
        await _handle_pr_event(
            db, redis, event_bus, project_id,
            action=payload.get("action", ""),
            pr_number=pr.get("number"),
            merged=pr.get("merged", False),
            merge_commit_sha=pr.get("merge_commit_sha"),
            pr_title=pr.get("title", ""),
            branch_name=pr.get("head", {}).get("ref", ""),
            repo_url=pr.get("base", {}).get("repo", {}).get("html_url", ""),
        )
    elif event_type == "ping":
        return {"status": "pong"}

    return {"status": "ok"}


# ------------------------------------------------------------------
# GitLab webhook
# ------------------------------------------------------------------

@router.post("/webhooks/gitlab/{project_id}")
async def gitlab_webhook(project_id: str, request: Request):
    """Receive GitLab webhook events."""
    db = request.app.state.db
    redis = request.app.state.redis
    event_bus = request.app.state.event_bus

    body = await request.body()

    # Verify token
    secret = await _get_webhook_secret(db, project_id, "gitlab")
    if secret:
        token = request.headers.get("X-Gitlab-Token")
        if not _verify_gitlab_token(secret, token):
            raise HTTPException(status_code=403, detail="Invalid token")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = request.headers.get("X-Gitlab-Event", "")

    logger.info(
        "[webhook] gitlab event=%s project=%s",
        event_type, project_id[:8],
    )

    if event_type == "Merge Request Hook":
        attrs = payload.get("object_attributes", {})
        source = attrs.get("source", {})
        await _handle_pr_event(
            db, redis, event_bus, project_id,
            action=attrs.get("action", ""),
            pr_number=attrs.get("iid"),
            merged=(attrs.get("state") == "merged"),
            merge_commit_sha=attrs.get("merge_commit_sha"),
            pr_title=attrs.get("title", ""),
            branch_name=attrs.get("source_branch", ""),
            repo_url=source.get("web_url", ""),
        )

    return {"status": "ok"}


# ------------------------------------------------------------------
# Shared event handlers
# ------------------------------------------------------------------

async def _handle_pr_event(
    db,
    redis,
    event_bus,
    project_id: str,
    *,
    action: str,
    pr_number: int | None,
    merged: bool,
    merge_commit_sha: str | None = None,
    pr_title: str = "",
    branch_name: str = "",
    repo_url: str = "",
) -> None:
    """Process a PR merge/close event.

    Finds the matching deliverable and wakes any merge_observer subtask.
    Also triggers the standalone merge pipeline if enabled.
    """
    if not pr_number:
        return

    # Only care about close/merge actions
    if action not in ("closed", "merge", "merged") and not merged:
        return

    # Find the deliverable by project + PR number
    deliv = await db.fetchrow(
        """
        SELECT d.*, t.id AS todo_id
        FROM deliverables d
        JOIN todo_items t ON d.todo_id = t.id
        WHERE t.project_id = $1
          AND d.type = 'pull_request'
          AND d.pr_number = $2
        ORDER BY d.created_at DESC LIMIT 1
        """,
        project_id, pr_number,
    )

    if deliv:
        todo_id = str(deliv["todo_id"])

        if merged:
            await db.execute(
                "UPDATE deliverables SET pr_state = 'merged', merged_at = NOW(), "
                "status = 'approved' WHERE id = $1",
                deliv["id"],
            )
            logger.info("[webhook] PR #%d marked as merged for todo %s", pr_number, todo_id[:8])

        # Wake the merge_observer via Redis signal
        await redis.set(
            f"merge_observer:{todo_id}:wake",
            json.dumps({
                "pr_number": pr_number,
                "merged": merged,
                "sha": merge_commit_sha or "",
            }),
            ex=86400,  # 24h TTL
        )

        # Publish event to wake the orchestrator if observer isn't actively running
        from agents.orchestrator.events import TaskEvent

        await event_bus.publish(TaskEvent(
            event_type="pr_merged" if merged else "pr_closed",
            todo_id=todo_id,
            state="in_progress",
            metadata={"pr_number": pr_number, "merge_commit_sha": merge_commit_sha or ""},
        ))

        logger.info(
            "[webhook] PR #%d %s for todo %s — observer woken",
            pr_number, "merged" if merged else "closed", todo_id[:8],
        )
    else:
        logger.info(
            "[webhook] No deliverable found for PR #%d in project %s",
            pr_number, project_id[:8],
        )

    # Trigger post-merge hooks (independent of task system)
    if merged:
        from agents.orchestrator.merge_pipeline import (
            run_post_merge_actions,
            trigger_merge_pipeline,
        )

        merge_kwargs = dict(
            pr_number=pr_number, pr_title=pr_title, branch_name=branch_name,
            commit_hash=merge_commit_sha or "", repo_url=repo_url,
        )

        # Post-merge actions (webhook calls, scripts) — fire-and-forget
        asyncio.create_task(
            run_post_merge_actions(db, redis, project_id, **merge_kwargs)
        )

        # Full merge pipeline (test verification → deploy)
        run_id = await trigger_merge_pipeline(
            db, redis, project_id, **merge_kwargs,
        )
        if run_id:
            logger.info(
                "[webhook] Merge pipeline triggered: run=%s PR=#%d project=%s",
                run_id[:8], pr_number, project_id[:8],
            )
