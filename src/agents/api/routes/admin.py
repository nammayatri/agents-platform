import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, AdminUser

logger = logging.getLogger(__name__)

router = APIRouter()


class UpdateRoleInput(BaseModel):
    role: str


class SystemSettingsInput(BaseModel):
    value_json: dict


@router.get("/todos")
async def admin_list_todos(user: AdminUser, db: DB, state: str | None = None):
    query = """
        SELECT t.*, u.display_name as creator_name, p.name as project_name
        FROM todo_items t
        JOIN users u ON t.creator_id = u.id
        JOIN projects p ON t.project_id = p.id
    """
    params: list = []
    if state:
        params.append(state)
        query += f" WHERE t.state = ${len(params)}"
    query += " ORDER BY t.created_at DESC LIMIT 200"

    rows = await db.fetch(query, *params)
    return [dict(r) for r in rows]


@router.get("/users")
async def admin_list_users(user: AdminUser, db: DB):
    rows = await db.fetch(
        "SELECT id, email, display_name, role, created_at FROM users ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


@router.get("/stats")
async def admin_stats(user: AdminUser, db: DB):
    stats = {}
    stats["total_users"] = await db.fetchval("SELECT COUNT(*) FROM users")
    stats["total_todos"] = await db.fetchval("SELECT COUNT(*) FROM todo_items")
    stats["active_todos"] = await db.fetchval(
        "SELECT COUNT(*) FROM todo_items WHERE state IN ('intake', 'planning', 'in_progress')"
    )
    stats["completed_todos"] = await db.fetchval(
        "SELECT COUNT(*) FROM todo_items WHERE state = 'completed'"
    )
    stats["failed_todos"] = await db.fetchval(
        "SELECT COUNT(*) FROM todo_items WHERE state = 'failed'"
    )
    stats["total_agent_runs"] = await db.fetchval("SELECT COUNT(*) FROM agent_runs")
    stats["total_tokens"] = await db.fetchval(
        "SELECT COALESCE(SUM(actual_tokens), 0) FROM todo_items"
    )
    stats["total_cost"] = await db.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM todo_items"
    )
    return stats


@router.get("/audit-log")
async def admin_audit_log(user: AdminUser, db: DB, limit: int = 100, offset: int = 0):
    rows = await db.fetch(
        "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    return [dict(r) for r in rows]


@router.put("/users/{user_id}/role")
async def admin_update_user_role(
    user_id: str, body: UpdateRoleInput, user: AdminUser, db: DB
):
    if body.role not in ("user", "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be 'user' or 'admin'",
        )

    if str(user["id"]) == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own role",
        )

    target = await db.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    await db.execute(
        "UPDATE users SET role = $2 WHERE id = $1",
        user_id,
        body.role,
    )
    row = await db.fetchrow(
        "SELECT id, email, display_name, role, created_at FROM users WHERE id = $1",
        user_id,
    )
    return dict(row)


# ── System Settings ──────────────────────────────────────────────────


@router.get("/settings/{key}")
async def get_system_setting(key: str, user: AdminUser, db: DB):
    row = await db.fetchrow(
        "SELECT key, value_json FROM system_settings WHERE key = $1", key
    )
    if not row:
        return {"key": key, "value_json": {}}
    return dict(row)


@router.put("/settings/{key}")
async def put_system_setting(
    key: str, body: SystemSettingsInput, user: AdminUser, db: DB
):
    row = await db.fetchrow(
        """
        INSERT INTO system_settings (key, value_json, updated_by)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (key) DO UPDATE
            SET value_json = $2::jsonb, updated_by = $3, updated_at = NOW()
        RETURNING *
        """,
        key,
        json.dumps(body.value_json),
        user["id"],
    )
    return dict(row)


@router.post("/settings/email/test")
async def test_email_config(user: AdminUser, db: DB):
    """Send a test email using the stored SMTP config."""
    from agents.infra.notifier import Notifier

    notifier = Notifier(db)
    admin_email = user.get("email", "")
    if not admin_email:
        return {"status": "error", "detail": "No email on your account"}
    try:
        await notifier._send_email(
            {"email": admin_email},
            "test",
            {
                "title": "Test Notification",
                "todo_id": "00000000",
                "detail": "This is a test from the Agent Orchestration Platform. If you see this, email is working.",
            },
        )
        await notifier.close()
        return {"status": "ok"}
    except Exception as e:
        await notifier.close()
        logger.exception("Email test failed")
        return {"status": "error", "detail": str(e)}
