"""Merge Pipeline API — CRUD for pipeline runs, settings, and test result webhook."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, Redis, check_project_access, check_project_owner

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic Models ──────────────────────────────────────────────────


class MergePipelineTestConfig(BaseModel):
    mode: str = "poll"  # "poll" | "webhook"
    poll_url: str | None = None
    poll_interval_seconds: int = 10
    poll_timeout_minutes: int = 15
    poll_success_value: str = "passed"
    poll_headers: dict[str, str] | None = None
    timeout_minutes: int = 30


class MergePipelineDeployConfig(BaseModel):
    enabled: bool = False
    deploy_type: str = "http"  # "http" | "kubernetes"
    api_url: str | None = None
    http_method: str = "POST"
    headers: dict[str, str] | None = None
    body_template: str | None = None
    success_status_codes: list[int] | None = None
    kube_commands: list[str] | None = None
    kube_context: str | None = None


class MergePipelineConfig(BaseModel):
    enabled: bool = False
    test_config: MergePipelineTestConfig | None = None
    deploy_config: MergePipelineDeployConfig | None = None


class MergePipelineSettingsInput(BaseModel):
    """Per-repo pipeline config. Key is repo name ('main' or dep name)."""
    merge_pipelines: dict[str, MergePipelineConfig] | None = None


# ── Post-Merge Action Models ────────────────────────────────────────


class PostMergeAction(BaseModel):
    type: str = "webhook"  # "webhook" | "script"
    # Webhook fields
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] | None = None
    body_template: str | None = None
    # Script fields
    command: str | None = None
    # Shared
    timeout_seconds: int = 30


class PostMergeRepoConfig(BaseModel):
    enabled: bool = False
    actions: list[PostMergeAction] | None = None


class PostMergeActionsInput(BaseModel):
    """Per-repo post-merge action config. Key is repo name."""
    post_merge_actions: dict[str, PostMergeRepoConfig] | None = None


# ── Settings Endpoints ───────────────────────────────────────────────


@router.get("/projects/{project_id}/merge-pipeline-settings")
async def get_merge_pipeline_settings(project_id: str, user: CurrentUser, db: DB):
    """Get merge pipeline configuration (per-repo)."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT settings_json, repo_url, context_docs FROM projects WHERE id = $1", project_id,
    )
    if not row:
        raise HTTPException(status_code=404)
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)

    # Build list of available repos
    repos = [{"name": "main", "repo_url": row.get("repo_url") or ""}]
    context_docs = row.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)
    for doc in context_docs:
        if isinstance(doc, dict) and doc.get("name") and doc.get("repo_url"):
            repos.append({"name": doc["name"], "repo_url": doc["repo_url"]})

    return {
        "merge_pipelines": settings.get("merge_pipelines", {}),
        "repos": repos,
    }


@router.put("/projects/{project_id}/merge-pipeline-settings")
async def update_merge_pipeline_settings(
    project_id: str, body: MergePipelineSettingsInput, user: CurrentUser, db: DB,
):
    """Update merge pipeline configuration (per-repo). Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)

    if body.merge_pipelines is not None:
        settings["merge_pipelines"] = {
            k: v.model_dump(exclude_none=True) for k, v in body.merge_pipelines.items()
        }

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id, settings,
    )
    return {"merge_pipelines": settings.get("merge_pipelines", {})}


# ── Pipeline Runs ────────────────────────────────────────────────────


@router.get("/projects/{project_id}/pipeline-runs")
async def list_pipeline_runs(
    project_id: str, user: CurrentUser, db: DB, limit: int = 30, offset: int = 0,
):
    """List pipeline runs for a project."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        "SELECT * FROM pipeline_runs WHERE project_id = $1 "
        "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        project_id, limit, offset,
    )
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/pipeline-runs/{run_id}")
async def get_pipeline_run(project_id: str, run_id: str, user: CurrentUser, db: DB):
    """Get a single pipeline run."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT * FROM pipeline_runs WHERE id = $1 AND project_id = $2",
        run_id, project_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return dict(row)


@router.post("/projects/{project_id}/pipeline-runs/{run_id}/cancel")
async def cancel_pipeline_run(
    project_id: str, run_id: str, user: CurrentUser, db: DB, redis: Redis,
):
    """Cancel an in-progress pipeline run."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT status FROM pipeline_runs WHERE id = $1 AND project_id = $2",
        run_id, project_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] not in ("pending", "testing", "deploying"):
        raise HTTPException(status_code=400, detail="Run is not in a cancellable state")

    await db.execute(
        "UPDATE pipeline_runs SET status = 'cancelled', updated_at = NOW() WHERE id = $1",
        run_id,
    )

    if redis:
        await redis.publish(
            f"pipeline:{project_id}:events",
            json.dumps({"type": "pipeline_status", "run_id": run_id, "status": "cancelled"}),
        )

    return {"status": "cancelled"}


# ── Available Template Variables ─────────────────────────────────────


@router.get("/projects/{project_id}/pipeline-variables")
async def get_pipeline_variables(project_id: str, user: CurrentUser, db: DB):
    """Return available template variables for pipeline configuration."""
    await check_project_access(db, project_id, user)
    from agents.orchestrator.merge_pipeline import AVAILABLE_VARIABLES
    return {
        "variables": [
            {"key": v, "example": _variable_example(v)}
            for v in AVAILABLE_VARIABLES
        ]
    }


def _variable_example(key: str) -> str:
    examples = {
        "commit_hash": "a1b2c3d4e5f6",
        "branch_name": "feature/my-feature",
        "pr_number": "42",
        "pr_title": "Add user authentication",
        "repo_url": "https://github.com/org/repo",
        "project_name": "My Project",
    }
    return examples.get(key, "")


# ── Post-Merge Actions ──────────────────────────────────────────────


@router.get("/projects/{project_id}/post-merge-actions")
async def get_post_merge_actions(project_id: str, user: CurrentUser, db: DB):
    """Get post-merge action configuration (per-repo)."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT settings_json, repo_url, context_docs FROM projects WHERE id = $1", project_id,
    )
    if not row:
        raise HTTPException(status_code=404)
    from agents.utils.settings_helpers import parse_settings, read_setting
    settings = parse_settings(row["settings_json"])

    repos = [{"name": "main", "repo_url": row.get("repo_url") or ""}]
    context_docs = row.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)
    for doc in context_docs:
        if isinstance(doc, dict) and doc.get("name") and doc.get("repo_url"):
            repos.append({"name": doc["name"], "repo_url": doc["repo_url"]})

    return {
        "post_merge_actions": read_setting(settings, "git.post_merge_actions", "post_merge_actions", {}),
        "repos": repos,
    }


@router.put("/projects/{project_id}/post-merge-actions")
async def update_post_merge_actions(
    project_id: str, body: PostMergeActionsInput, user: CurrentUser, db: DB,
):
    """Update post-merge action configuration (per-repo). Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    from agents.utils.settings_helpers import migrate_settings, parse_settings, read_setting
    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings)

    if body.post_merge_actions is not None:
        settings.setdefault("git", {})["post_merge_actions"] = {
            k: v.model_dump(exclude_none=True) for k, v in body.post_merge_actions.items()
        }

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id, settings,
    )
    return {"post_merge_actions": read_setting(settings, "git.post_merge_actions", "post_merge_actions", {})}


# ── Test Result Webhook (no auth — uses unique token) ────────────────


@router.post("/webhooks/pipeline-test/{webhook_token}")
async def receive_test_result(webhook_token: str, request: Request):
    """Receive test results from external CI via webhook callback.

    The webhook_token is a unique per-run secret generated when the pipeline
    starts in webhook mode. No JWT auth required.
    """
    db = request.app.state.db
    redis = request.app.state.redis

    row = await db.fetchrow(
        "SELECT id, project_id, status FROM pipeline_runs "
        "WHERE webhook_token = $1",
        webhook_token,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired webhook token")

    if row["status"] != "testing":
        raise HTTPException(
            status_code=400,
            detail=f"Pipeline run is not in testing state (current: {row['status']})",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Determine pass/fail from the payload
    passed = body.get("passed", body.get("success", body.get("status") == "passed"))
    result_data = {
        "passed": bool(passed),
        "output": body,
        "received_at": "now",
    }

    # Signal the executor via Redis
    run_id = str(row["id"])
    await redis.set(
        f"pipeline_test_result:{run_id}",
        json.dumps(result_data, default=str),
        ex=3600,
    )

    logger.info(
        "[pipeline] Test webhook received for run %s: passed=%s",
        run_id[:8], passed,
    )

    return {"status": "received", "run_id": run_id, "passed": bool(passed)}
