import asyncio
import json

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, check_project_access, check_project_owner

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
async def create_project(body: CreateProjectInput, user: CurrentUser, db: DB):
    context_docs_json = json.dumps(
        [d.model_dump(exclude_none=True) for d in body.context_docs]
    ) if body.context_docs else "[]"

    row = await db.fetchrow(
        """
        INSERT INTO projects (
            owner_id, name, description, repo_url, default_branch,
            ai_provider_id, context_docs, git_provider_id, icon_url
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9) RETURNING *
        """,
        user["id"],
        body.name,
        body.description,
        body.repo_url,
        body.default_branch,
        body.ai_provider_id,
        context_docs_json,
        body.git_provider_id,
        body.icon_url,
    )
    result = _sanitize_project(dict(row))

    # Kick off async project analysis if repo_url is provided
    if body.repo_url:
        asyncio.create_task(_analyze_project(str(row["id"]), db))

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
async def update_project(project_id: str, body: UpdateProjectInput, user: CurrentUser, db: DB):
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not row:
        raise HTTPException(status_code=404)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return _sanitize_project(dict(row))

    # Serialize context_docs to JSON string for JSONB column
    if "context_docs" in updates:
        updates["context_docs"] = json.dumps(
            [d if isinstance(d, dict) else d for d in updates["context_docs"]]
        )

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        if k == "context_docs":
            set_parts.append(f"{k} = ${i+2}::jsonb")
        else:
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
        old_deps_str = json.dumps(old_deps) if old_deps else "[]"
        new_deps_str = updates["context_docs"] if isinstance(updates["context_docs"], str) else json.dumps(updates["context_docs"])
        if new_deps_str != old_deps_str:
            should_reanalyze = True
    if "git_provider_id" in updates and str(updates["git_provider_id"] or "") != str(old_git or ""):
        should_reanalyze = True

    if should_reanalyze and result.get("repo_url"):
        asyncio.create_task(_analyze_project(project_id, db))

    return result


@router.post("/{project_id}/analyze")
async def analyze_project(project_id: str, user: CurrentUser, db: DB):
    """Manually trigger project analysis."""
    await check_project_access(db, project_id, user)
    row = await db.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    if not row["repo_url"]:
        raise HTTPException(status_code=400, detail="Project has no repository URL")

    asyncio.create_task(_analyze_project(project_id, db))
    return {"status": "analyzing"}


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
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)
    return settings.get("work_rules", {})


@router.put("/{project_id}/rules")
async def update_work_rules(project_id: str, body: WorkRulesInput, user: CurrentUser, db: DB):
    """Update project-level work rules. Owner-only. Merges by category."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)

    rules = settings.get("work_rules", {})
    for category, values in body.model_dump(exclude_none=True).items():
        rules[category] = values
    settings["work_rules"] = rules

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
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)
    return settings.get("debug_context", {})


@router.put("/{project_id}/debug-context")
async def update_debug_context(
    project_id: str, body: DebugContextInput, user: CurrentUser, db: DB,
):
    """Update project-level debug context. Owner-only."""
    await check_project_owner(db, project_id, user)
    row = await db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", project_id)
    settings = row["settings_json"] or {}
    if isinstance(settings, str):
        settings = json.loads(settings)

    settings["debug_context"] = body.model_dump(exclude_none=True)

    await db.execute(
        "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
        project_id,
        settings,
    )
    return settings["debug_context"]


async def _analyze_project(project_id: str, db) -> None:
    """Background task to analyze a project's repo and store understanding."""
    from agents.orchestrator.project_analyzer import ProjectAnalyzer

    try:
        analyzer = ProjectAnalyzer(db)
        await analyzer.analyze(project_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Background project analysis failed for %s", project_id)
