import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, Redis, check_project_access, check_project_owner
from agents.utils.settings_helpers import (
    VALID_SECTIONS,
    get_build_command_strings,
    migrate_settings,
    parse_settings,
    read_setting,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ProjectDependency(BaseModel):
    name: str
    repo_url: str | None = None
    description: str | None = None
    git_provider_id: str | None = None  # reference to git_provider_configs


class CreateProjectInput(BaseModel):
    name: str
    description: str | None = None
    repo_url: str | None = None
    default_branch: str = "main"
    ai_provider_id: str | None = None
    context_docs: list[ProjectDependency] | None = None
    git_provider_id: str | None = None  # reference to git_provider_configs
    icon_url: str | None = None


class UpdateProjectInput(BaseModel):
    name: str | None = None
    description: str | None = None
    repo_url: str | None = None
    default_branch: str | None = None
    ai_provider_id: str | None = None
    context_docs: list[ProjectDependency] | None = None
    git_provider_id: str | None = None
    icon_url: str | None = None
    # Legacy fields — kept for backwards compat; prefer PUT /settings/execution
    architect_editor_enabled: bool | None = None
    architect_model: str | None = None
    editor_model: str | None = None


def _sanitize_project(row: dict) -> dict:
    """Remove sensitive fields from project response."""
    result = dict(row)
    # Remove legacy inline token field if present
    result.pop("git_token_enc", None)
    return result


@router.get("")
async def list_projects(user: CurrentUser, db: DB):
    rows = await db.fetch(
        """
        SELECT p.*, 'owner' AS user_role FROM projects p WHERE p.owner_id = $1
        UNION ALL
        SELECT p.*, 'member' AS user_role FROM projects p
        JOIN project_members pm ON pm.project_id = p.id
        WHERE pm.user_id = $1
        ORDER BY created_at DESC
        """,
        user["id"],
    )
    return [_sanitize_project(dict(r)) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProjectInput, user: CurrentUser, db: DB, redis: Redis):
    context_docs_list = [
        d.model_dump(exclude_none=True) for d in body.context_docs
    ] if body.context_docs else []

    row = await db.fetchrow(
        """
        INSERT INTO projects (
            owner_id, name, description, repo_url, default_branch,
            ai_provider_id, context_docs, git_provider_id, icon_url
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *
        """,
        user["id"],
        body.name,
        body.description,
        body.repo_url,
        body.default_branch,
        body.ai_provider_id,
        context_docs_list,
        body.git_provider_id,
        body.icon_url,
    )
    result = _sanitize_project(dict(row))

    # Copy pipeline configs from an existing project with the same repo
    if body.repo_url:
        await _copy_pipeline_configs_from_existing(db, str(row["id"]), body.repo_url)

    # Kick off async project analysis if repo_url is provided
    if body.repo_url:
        asyncio.create_task(_analyze_project(str(row["id"]), db, redis))

    return result


@router.get("/{project_id}")
async def get_project(project_id: str, user: CurrentUser, db: DB):
    role = await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    result = _sanitize_project(dict(row))
    result["user_role"] = role
    return result


@router.put("/{project_id}")
async def update_project(project_id: str, body: UpdateProjectInput, user: CurrentUser, db: DB, redis: Redis):
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return _sanitize_project(dict(row))

    # Ensure context_docs entries are plain dicts (not Pydantic models)
    if "context_docs" in updates:
        updates["context_docs"] = [
            d.model_dump(exclude_none=True) if hasattr(d, "model_dump") else d
            for d in updates["context_docs"]
        ]

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i+2}")
        values.append(v)

    set_clause = ", ".join(set_parts)

    # Capture old values before update for change detection
    old_repo = row.get("repo_url")
    old_deps = row.get("context_docs")
    old_git = row.get("git_provider_id")

    updated = await db.fetchrow(
        f"UPDATE projects SET {set_clause}, updated_at = NOW() WHERE id = $1 RETURNING *",
        project_id,
        *values,
    )
    result = _sanitize_project(dict(updated))

    # Only re-analyze if relevant values actually changed
    should_reanalyze = False
    if "repo_url" in updates and updates["repo_url"] != old_repo:
        should_reanalyze = True
    if "context_docs" in updates:
        old_deps_norm = json.dumps(old_deps, sort_keys=True) if old_deps else "[]"
        new_deps_norm = json.dumps(updates["context_docs"], sort_keys=True) if updates["context_docs"] else "[]"
        if new_deps_norm != old_deps_norm:
            should_reanalyze = True
    if "git_provider_id" in updates and str(updates["git_provider_id"] or "") != str(old_git or ""):
        should_reanalyze = True

    if should_reanalyze and result.get("repo_url"):
        asyncio.create_task(_analyze_project(project_id, db, redis))

    return result


@router.post("/{project_id}/analyze")
async def analyze_project(project_id: str, user: CurrentUser, db: DB, redis: Redis):
    """Manually trigger project analysis."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not row["repo_url"]:
        raise HTTPException(status_code=400, detail="Project has no repository URL")

    asyncio.create_task(_analyze_project(project_id, db, redis))
    return {"status": "analyzing"}


@router.post("/{project_id}/cancel-analysis")
async def cancel_analysis(project_id: str, user: CurrentUser, db: DB):
    """Cancel/reset a stuck analysis."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    settings = parse_settings(row["settings_json"])
    status_val = read_setting(settings, "understanding.status", "analysis_status")
    if status_val == "analyzing":
        # Update in whichever format the settings are currently in
        if isinstance(settings.get("understanding"), dict):
            settings["understanding"]["status"] = None
        else:
            settings["analysis_status"] = None
        await db.execute(
            "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            settings,
        )
    return {"status": "cancelled"}


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, user: CurrentUser, db: DB):
    await check_project_owner(db, project_id, user)
    await db.execute("DELETE FROM projects WHERE id = $1", project_id)


# ── Member Management ──────────────────────────────────────────


class AddMemberInput(BaseModel):
    email: str


@router.get("/{project_id}/members")
async def list_members(project_id: str, user: CurrentUser, db: DB):
    """List project owner and members."""
    await check_project_access(db, project_id, user)

    project = await db.fetchrow("SELECT owner_id FROM projects WHERE id = $1", project_id)
    owner = await db.fetchrow(
        "SELECT id, email, display_name, avatar_url FROM users WHERE id = $1",
        project["owner_id"],
    )

    rows = await db.fetch(
        """
        SELECT u.id, u.email, u.display_name, u.avatar_url, pm.role, pm.created_at
        FROM project_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.project_id = $1
        ORDER BY pm.created_at ASC
        """,
        project_id,
    )

    return {
        "owner": {
            "id": str(owner["id"]),
            "email": owner["email"],
            "display_name": owner["display_name"],
            "avatar_url": owner.get("avatar_url"),
            "role": "owner",
        },
        "members": [
            {
                "id": str(r["id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "avatar_url": r.get("avatar_url"),
                "role": r["role"],
                "added_at": str(r["created_at"]),
            }
            for r in rows
        ],
    }


@router.post("/{project_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(project_id: str, body: AddMemberInput, user: CurrentUser, db: DB):
    """Add a member by email. Owner-only."""
    await check_project_owner(db, project_id, user)

    target = await db.fetchrow(
        "SELECT id, email, display_name FROM users WHERE email = $1",
        body.email.strip().lower(),
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found. They must register first.")

    project = await db.fetchrow("SELECT owner_id FROM projects WHERE id = $1", project_id)
    if str(target["id"]) == str(project["owner_id"]):
        raise HTTPException(status_code=400, detail="Cannot add the project owner as a member")

    existing = await db.fetchrow(
        "SELECT id FROM project_members WHERE project_id = $1 AND user_id = $2",
        project_id, target["id"],
    )
    if existing:
        raise HTTPException(status_code=409, detail="User is already a member")

    await db.execute(
        "INSERT INTO project_members (project_id, user_id, added_by) VALUES ($1, $2, $3)",
        project_id, target["id"], user["id"],
    )

    return {
        "id": str(target["id"]),
        "email": target["email"],
        "display_name": target["display_name"],
        "role": "member",
    }


@router.delete("/{project_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(project_id: str, member_user_id: str, user: CurrentUser, db: DB):
    """Remove a member. Owner-only."""
    await check_project_owner(db, project_id, user)
    result = await db.execute(
        "DELETE FROM project_members WHERE project_id = $1 AND user_id = $2",
        project_id, member_user_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Member not found")


# ── Work Rules ─────────────────────────────────────────────


class WorkRulesInput(BaseModel):
    coding: list[str] | None = None
    testing: list[str] | None = None
    review: list[str] | None = None
    quality: list[str] | None = None
    general: list[str] | None = None


# ── Debug Context ──────────────────────────────────────────


class DebugLogSource(BaseModel):
    service_name: str
    log_path: str | None = None
    log_command: str | None = None
    description: str | None = None


class DebugMcpHint(BaseModel):
    mcp_server_name: str
    available_data: list[str] | None = None
    example_queries: list[str] | None = None
    notes: str | None = None


class DebugContextInput(BaseModel):
    log_sources: list[DebugLogSource] | None = None
    mcp_hints: list[DebugMcpHint] | None = None
    custom_instructions: str | None = None


@router.get("/{project_id}/rules")
async def get_work_rules(project_id: str, user: CurrentUser, db: DB):
    """Get project-level work rules."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = parse_settings(row["settings_json"])
    return read_setting(settings, "execution.work_rules", "work_rules", {})


@router.put("/{project_id}/rules")
async def update_work_rules(project_id: str, body: WorkRulesInput, user: CurrentUser, db: DB):
    """Update project-level work rules. Owner-only. Merges by category."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings)

    rules = settings.get("execution", {}).get("work_rules", {})
    for category, values in body.model_dump(exclude_none=True).items():
        rules[category] = values
    settings.setdefault("execution", {})["work_rules"] = rules

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return rules


@router.get("/{project_id}/debug-context")
async def get_debug_context(project_id: str, user: CurrentUser, db: DB):
    """Get project-level debug context (log sources, MCP hints, instructions)."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = parse_settings(row["settings_json"])
    return read_setting(settings, "debugging", "debug_context", {})


@router.put("/{project_id}/debug-context")
async def update_debug_context(
    project_id: str, body: DebugContextInput, user: CurrentUser, db: DB,
):
    """Update project-level debug context. Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings)

    settings["debugging"] = body.model_dump(exclude_none=True)

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return settings["debugging"]


class BuildSettingsInput(BaseModel):
    build_commands: list[str] | None = None
    merge_method: str | None = None
    require_merge_approval: bool | None = None
    require_plan_approval: bool | None = None


# ── Release Pipeline Settings ─────────────────────────────────────


class ReleaseEndpointConfig(BaseModel):
    enabled: bool = False
    api_url: str | None = None
    http_method: str = "POST"
    headers: dict[str, str] | None = None
    body_template: str | None = None
    success_status_codes: list[int] | None = None
    poll_status_url: str | None = None
    poll_success_value: str | None = None


class ProdReleaseEndpointConfig(ReleaseEndpointConfig):
    require_approval: bool = False


class BuildConfig(BaseModel):
    workflow_name: str | None = None      # GitHub Actions
    job_url: str | None = None            # Jenkins
    token: str | None = None              # Jenkins auth token
    timeout_minutes: int = 30
    poll_interval_seconds: int = 30


class ReleaseConfig(BaseModel):
    build_provider: str = "github_actions"  # "github_actions" | "jenkins"
    build_config: BuildConfig | None = None
    test_release: ReleaseEndpointConfig | None = None
    prod_release: ProdReleaseEndpointConfig | None = None


class ReleaseSettingsInput(BaseModel):
    """Per-repo release config. Key is repo name ('main' or dep name)."""
    release_pipeline_enabled: bool | None = None
    release_configs: dict[str, ReleaseConfig] | None = None


@router.get("/{project_id}/build-settings")
async def get_build_settings(project_id: str, user: CurrentUser, db: DB):
    """Get project build & merge settings."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = parse_settings(row["settings_json"])
    return {
        "build_commands": read_setting(settings, "git.build_commands", "build_commands", []),
        "merge_method": read_setting(settings, "git.merge_method", "merge_method", "squash"),
        "require_merge_approval": read_setting(settings, "git.require_merge_approval", "require_merge_approval", False),
        "require_plan_approval": read_setting(settings, "planning.require_approval", "require_plan_approval", False),
    }


@router.put("/{project_id}/build-settings")
async def update_build_settings(
    project_id: str, body: BuildSettingsInput, user: CurrentUser, db: DB,
):
    """Update project build & merge settings. Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings)

    git = settings.setdefault("git", {})
    planning = settings.setdefault("planning", {})

    if body.build_commands is not None:
        git["build_commands"] = body.build_commands
    if body.merge_method is not None:
        git["merge_method"] = body.merge_method
    if body.require_merge_approval is not None:
        git["require_merge_approval"] = body.require_merge_approval
    if body.require_plan_approval is not None:
        planning["require_approval"] = body.require_plan_approval

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return {
        "build_commands": git.get("build_commands", []),
        "merge_method": git.get("merge_method", "squash"),
        "require_merge_approval": git.get("require_merge_approval", False),
        "require_plan_approval": planning.get("require_approval", False),
    }


@router.get("/{project_id}/release-settings")
async def get_release_settings(project_id: str, user: CurrentUser, db: DB):
    """Get project release pipeline settings (per-repo)."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT settings_json, repo_url, context_docs FROM projects WHERE id = $1", project_id,
    )
    if not row:
        raise HTTPException(status_code=404)
    settings = parse_settings(row["settings_json"])

    # Backwards compat: migrate old single release_config to per-repo dict
    release_configs = settings.get("release_configs", {})
    if not release_configs and settings.get("release_config"):
        release_configs = {"main": settings["release_config"]}

    repos = _build_repo_list(row)

    return {
        "release_pipeline_enabled": read_setting(settings, "release.enabled", "release_pipeline_enabled", False),
        "release_configs": release_configs,
        "repos": repos,
    }


@router.put("/{project_id}/release-settings")
async def update_release_settings(
    project_id: str, body: ReleaseSettingsInput, user: CurrentUser, db: DB,
):
    """Update project release pipeline settings (per-repo). Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)
    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings)

    if body.release_pipeline_enabled is not None:
        settings.setdefault("release", {})["enabled"] = body.release_pipeline_enabled
    if body.release_configs is not None:
        settings["release_configs"] = {
            k: v.model_dump(exclude_none=True) for k, v in body.release_configs.items()
        }
        # Keep legacy key in sync for backwards compat with task-based handlers
        if "main" in body.release_configs:
            settings["release_config"] = settings["release_configs"]["main"]

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return {
        "release_pipeline_enabled": read_setting(settings, "release.enabled", "release_pipeline_enabled", False),
        "release_configs": settings.get("release_configs", {}),
    }


# ── Unified Settings API ──────────────────────────────────────────


class SettingsSectionBody(BaseModel):
    """Arbitrary JSON body for a single settings section."""
    class Config:
        extra = "allow"


@router.get("/{project_id}/settings")
async def get_settings(project_id: str, user: CurrentUser, db: DB):
    """Get all project settings in the new structured format."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow(
        "SELECT settings_json, architect_editor_enabled, architect_model, editor_model "
        "FROM projects WHERE id = $1",
        project_id,
    )
    if not row:
        raise HTTPException(status_code=404)
    settings = parse_settings(row["settings_json"])
    migrated = migrate_settings(settings, project_row=dict(row))
    return migrated


@router.put("/{project_id}/settings/{section}")
async def update_settings_section(
    project_id: str, section: str, body: dict[str, Any],
    user: CurrentUser, db: DB,
):
    """Update a single settings section. Owner-only.

    section must be one of: planning, execution, git, debugging, release.
    The body is merged into the named section.
    """
    if section not in VALID_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid section '{section}'. Must be one of: {', '.join(sorted(VALID_SECTIONS))}",
        )
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow(
        "SELECT settings_json, architect_editor_enabled, architect_model, editor_model "
        "FROM projects WHERE id = $1",
        project_id,
    )
    if not row:
        raise HTTPException(status_code=404)

    settings = parse_settings(row["settings_json"])
    settings = migrate_settings(settings, project_row=dict(row))

    # Deep-merge the body into the target section
    existing_section = settings.get(section, {})
    if isinstance(existing_section, dict) and isinstance(body, dict):
        existing_section.update(body)
        settings[section] = existing_section
    else:
        settings[section] = body

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return settings[section]


def _build_repo_list(project_row) -> list[dict]:
    """Build a list of repos from project row (main + deps)."""
    repos = [{"name": "main", "repo_url": project_row.get("repo_url") or ""}]
    context_docs = project_row.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)
    for doc in context_docs:
        if isinstance(doc, dict) and doc.get("name") and doc.get("repo_url"):
            repos.append({"name": doc["name"], "repo_url": doc["repo_url"]})
    return repos


async def _copy_pipeline_configs_from_existing(db, new_project_id: str, repo_url: str) -> None:
    """Copy release_configs and merge_pipelines from an existing project with the same repo.

    When a user creates a new project pointing at a repo that already has
    pipeline configs in another project, auto-copy those configs so they
    don't have to reconfigure everything.  The user can still override per-project.
    """
    if not repo_url:
        return

    # Normalize URL for matching
    norm = repo_url.rstrip("/").removesuffix(".git").lower()

    # Find an existing project with matching repo that has pipeline configs
    donor = await db.fetchrow(
        """
        SELECT settings_json FROM projects
        WHERE id != $1
          AND LOWER(RTRIM(REPLACE(repo_url, '.git', ''), '/')) = $2
          AND settings_json IS NOT NULL
        ORDER BY updated_at DESC LIMIT 1
        """,
        new_project_id, norm,
    )
    if not donor:
        return

    donor_settings = parse_settings(donor["settings_json"])

    # Collect configs to copy (only if they exist in the donor)
    to_copy = {}
    for key in ("release_configs", "release_config", "release_pipeline_enabled",
                "merge_pipelines"):
        if key in donor_settings:
            to_copy[key] = donor_settings[key]
    # Also copy from new-format sections
    if "release" in donor_settings and isinstance(donor_settings["release"], dict):
        if "enabled" in donor_settings["release"]:
            to_copy.setdefault("release_pipeline_enabled", donor_settings["release"]["enabled"])
    if "git" in donor_settings and isinstance(donor_settings["git"], dict):
        if "post_merge_actions" in donor_settings["git"]:
            to_copy.setdefault("post_merge_actions", donor_settings["git"]["post_merge_actions"])

    if not to_copy:
        return

    # Merge into the new project's settings
    row = await db.fetchrow(
        "SELECT settings_json FROM projects WHERE id = $1", new_project_id,
    )
    settings = parse_settings((row or {}).get("settings_json"))

    settings.update(to_copy)

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        new_project_id, settings,
    )
    logger.info(
        "[project] Copied pipeline configs from existing project to new project %s (keys: %s)",
        new_project_id[:8], list(to_copy.keys()),
    )


# ── Project Memories ──────────────────────────────────────────


class UpdateMemoryInput(BaseModel):
    content: str | None = None
    category: str | None = None
    confidence: float | None = None


@router.get("/{project_id}/memories")
async def list_memories(project_id: str, user: CurrentUser, db: DB):
    """List project memories with optional category filter."""
    await check_project_access(db, project_id, user)
    rows = await db.fetch(
        """
        SELECT id, project_id, category, content, source_todo_id,
               confidence, created_at, updated_at
        FROM project_memories
        WHERE project_id = $1
        ORDER BY confidence DESC, created_at DESC
        """,
        project_id,
    )
    return [dict(r) for r in rows]


@router.put("/{project_id}/memories/{memory_id}")
async def update_memory(
    project_id: str, memory_id: str, body: UpdateMemoryInput,
    user: CurrentUser, db: DB,
):
    """Update a memory's content or category. Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow(
        "SELECT id FROM project_memories WHERE id = $1 AND project_id = $2",
        memory_id, project_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": "no changes"}

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i+3}")
        values.append(v)

    await db.execute(
        f"UPDATE project_memories SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1 AND project_id = $2",
        memory_id, project_id, *values,
    )
    return {"status": "ok"}


@router.delete("/{project_id}/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(project_id: str, memory_id: str, user: CurrentUser, db: DB):
    """Delete a memory. Owner-only."""
    await check_project_owner(db, project_id, user)
    result = await db.execute(
        "DELETE FROM project_memories WHERE id = $1 AND project_id = $2",
        memory_id, project_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Memory not found")


# ── Webhook Secrets ──────────────────────────────────────────


class WebhookSecretInput(BaseModel):
    provider_type: str  # 'github' | 'gitlab'


@router.post("/{project_id}/webhook-secrets", status_code=status.HTTP_201_CREATED)
async def create_webhook_secret(
    project_id: str, body: WebhookSecretInput, user: CurrentUser, db: DB,
):
    """Generate and store a webhook secret for a provider. Owner-only."""
    await check_project_owner(db, project_id, user)

    if body.provider_type not in ("github", "gitlab"):
        raise HTTPException(status_code=400, detail="provider_type must be 'github' or 'gitlab'")

    secret = secrets.token_hex(32)

    # Upsert: deactivate existing, insert new
    await db.execute(
        "UPDATE webhook_secrets SET is_active = FALSE WHERE project_id = $1 AND provider_type = $2",
        project_id, body.provider_type,
    )
    row = await db.fetchrow(
        """
        INSERT INTO webhook_secrets (project_id, provider_type, secret)
        VALUES ($1, $2, $3)
        RETURNING id, provider_type, is_active, created_at
        """,
        project_id, body.provider_type, secret,
    )

    return {
        "id": str(row["id"]),
        "provider_type": row["provider_type"],
        "secret": secret,
        "webhook_url": f"/api/webhooks/{body.provider_type}/{project_id}",
        "created_at": str(row["created_at"]),
    }


@router.get("/{project_id}/webhook-secrets")
async def list_webhook_secrets(project_id: str, user: CurrentUser, db: DB):
    """List webhook secrets (without exposing secret values). Owner-only."""
    await check_project_owner(db, project_id, user)
    rows = await db.fetch(
        """
        SELECT id, provider_type, is_active, created_at, updated_at
        FROM webhook_secrets
        WHERE project_id = $1
        ORDER BY created_at DESC
        """,
        project_id,
    )
    return [
        {
            "id": str(r["id"]),
            "provider_type": r["provider_type"],
            "is_active": r["is_active"],
            "webhook_url": f"/api/webhooks/{r['provider_type']}/{project_id}",
            "created_at": str(r["created_at"]),
            "updated_at": str(r["updated_at"]),
        }
        for r in rows
    ]


@router.delete("/{project_id}/webhook-secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook_secret(
    project_id: str, secret_id: str, user: CurrentUser, db: DB,
):
    """Delete a webhook secret. Owner-only."""
    await check_project_owner(db, project_id, user)
    result = await db.execute(
        "DELETE FROM webhook_secrets WHERE id = $1 AND project_id = $2",
        secret_id, project_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Webhook secret not found")


async def _analyze_project(project_id: str, db, redis=None) -> None:
    """Background task to analyze a project's repo and store understanding."""
    from agents.orchestrator.project_analyzer import ProjectAnalyzer

    try:
        analyzer = ProjectAnalyzer(db, redis=redis)
        await analyzer.analyze(project_id)
    except Exception:
        logger.exception("Background project analysis failed for %s", project_id)
