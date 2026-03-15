"""Notification channel management — CRUD + test for user notification channels."""

import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────


class NotificationChannelInput(BaseModel):
    channel_type: str  # 'email' | 'slack' | 'webhook'
    display_name: str
    config_json: dict  # {email, webhook_url, url, headers, api_url, api_key, ...}
    notify_on: list[str] = ["stuck", "failed", "completed", "review"]


class NotificationChannelUpdate(BaseModel):
    display_name: str | None = None
    config_json: dict | None = None
    notify_on: list[str] | None = None
    is_active: bool | None = None


# ── CRUD ─────────────────────────────────────────────────────────────


VALID_CHANNEL_TYPES = {"email", "slack", "webhook"}
VALID_EVENTS = {"stuck", "failed", "completed", "review", "in_progress"}


@router.get("/notifications/channels")
async def list_channels(user: CurrentUser, db: DB):
    rows = await db.fetch(
        "SELECT * FROM notification_channels WHERE user_id = $1 ORDER BY created_at DESC",
        user["id"],
    )
    return [dict(r) for r in rows]


@router.post("/notifications/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(body: NotificationChannelInput, user: CurrentUser, db: DB):
    if body.channel_type not in VALID_CHANNEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid channel_type. Must be one of: {', '.join(VALID_CHANNEL_TYPES)}")

    invalid_events = set(body.notify_on) - VALID_EVENTS
    if invalid_events:
        raise HTTPException(status_code=400, detail=f"Invalid events: {', '.join(invalid_events)}")

    row = await db.fetchrow(
        """
        INSERT INTO notification_channels (user_id, channel_type, display_name, config_json, notify_on)
        VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING *
        """,
        user["id"],
        body.channel_type,
        body.display_name,
        json.dumps(body.config_json),
        body.notify_on,
    )
    return dict(row)


@router.put("/notifications/channels/{channel_id}")
async def update_channel(channel_id: str, body: NotificationChannelUpdate, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT * FROM notification_channels WHERE id = $1", channel_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Channel not found")
    if str(existing["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    updates = {}
    values = []
    set_parts = []
    idx = 2  # $1 is channel_id

    if body.display_name is not None:
        set_parts.append(f"display_name = ${idx}")
        values.append(body.display_name)
        idx += 1

    if body.config_json is not None:
        set_parts.append(f"config_json = ${idx}::jsonb")
        values.append(json.dumps(body.config_json))
        idx += 1

    if body.notify_on is not None:
        invalid_events = set(body.notify_on) - VALID_EVENTS
        if invalid_events:
            raise HTTPException(status_code=400, detail=f"Invalid events: {', '.join(invalid_events)}")
        set_parts.append(f"notify_on = ${idx}")
        values.append(body.notify_on)
        idx += 1

    if body.is_active is not None:
        set_parts.append(f"is_active = ${idx}")
        values.append(body.is_active)
        idx += 1

    if not set_parts:
        return dict(existing)

    row = await db.fetchrow(
        f"UPDATE notification_channels SET {', '.join(set_parts)} WHERE id = $1 RETURNING *",
        channel_id,
        *values,
    )
    return dict(row)


@router.delete("/notifications/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT user_id FROM notification_channels WHERE id = $1", channel_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Channel not found")
    if str(existing["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.execute("DELETE FROM notification_channels WHERE id = $1", channel_id)


@router.post("/notifications/channels/{channel_id}/test")
async def test_channel(channel_id: str, user: CurrentUser, db: DB):
    """Send a test notification to verify the channel works."""
    row = await db.fetchrow("SELECT * FROM notification_channels WHERE id = $1", channel_id)
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    if str(row["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    from agents.infra.notifier import Notifier
    notifier = Notifier(db)

    channel = dict(row)
    config = (
        json.loads(channel["config_json"])
        if isinstance(channel["config_json"], str)
        else channel["config_json"]
    )

    try:
        match channel["channel_type"]:
            case "slack":
                await notifier._send_slack(config, "test", {
                    "title": "Test Notification",
                    "todo_id": "00000000",
                    "detail": "This is a test from the Agent Orchestration Platform.",
                })
            case "email":
                await notifier._send_email(config, "test", {
                    "title": "Test Notification",
                    "todo_id": "00000000",
                    "detail": "This is a test from the Agent Orchestration Platform.",
                })
            case "webhook":
                await notifier._send_webhook(config, "test", {
                    "title": "Test Notification",
                    "todo_id": "00000000",
                    "detail": "This is a test from the Agent Orchestration Platform.",
                })
        await notifier.close()
        return {"status": "ok"}
    except Exception as e:
        await notifier.close()
        return {"status": "error", "detail": str(e)}
