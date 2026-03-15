from typing import Annotated, Literal

import asyncpg
import jwt
import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agents.config.settings import settings

security = HTTPBearer()


def get_db(request: Request) -> asyncpg.Pool:
    return request.app.state.db


def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[asyncpg.Pool, Depends(get_db)],
) -> dict:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return dict(user)


async def require_admin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


def get_event_bus(request: Request):
    from agents.orchestrator.events import EventBus
    return request.app.state.event_bus


DB = Annotated[asyncpg.Pool, Depends(get_db)]
Redis = Annotated[aioredis.Redis, Depends(get_redis)]
CurrentUser = Annotated[dict, Depends(get_current_user)]
AdminUser = Annotated[dict, Depends(require_admin)]

from agents.orchestrator.events import EventBus as _EventBus  # noqa: E402
EventBusDep = Annotated[_EventBus, Depends(get_event_bus)]


# ── Project Access Helpers ───────────────────────────────────────


async def check_project_access(
    db: asyncpg.Pool,
    project_id: str,
    user: dict,
) -> Literal["owner", "member"]:
    """Check if user can access a project. Returns role or raises 403/404."""
    project = await db.fetchrow(
        "SELECT owner_id FROM projects WHERE id = $1", project_id
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if str(project["owner_id"]) == str(user["id"]) or user["role"] == "admin":
        return "owner"

    member = await db.fetchrow(
        "SELECT role FROM project_members WHERE project_id = $1 AND user_id = $2",
        project_id,
        user["id"],
    )
    if member:
        return "member"

    raise HTTPException(status_code=403, detail="Access denied")


async def check_project_owner(
    db: asyncpg.Pool,
    project_id: str,
    user: dict,
) -> None:
    """Assert user is the project owner. Raises 403/404."""
    project = await db.fetchrow(
        "SELECT owner_id FROM projects WHERE id = $1", project_id
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if str(project["owner_id"]) != str(user["id"]) and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Owner access required")
