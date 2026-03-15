from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, status

from agents.api.deps import DB, CurrentUser
from agents.config.settings import settings
from agents.schemas.auth import LoginInput, RegisterInput, TokenResponse, UserOut

router = APIRouter()


def _create_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {"sub": user_id, "exp": expire},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterInput, db: DB):
    existing = await db.fetchrow("SELECT id FROM users WHERE email = $1", body.email)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = await db.fetchrow(
        """
        INSERT INTO users (email, display_name, password_hash)
        VALUES ($1, $2, $3) RETURNING id
        """,
        body.email,
        body.display_name,
        password_hash,
    )
    return TokenResponse(access_token=_create_token(str(user["id"])))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginInput, db: DB):
    user = await db.fetchrow("SELECT * FROM users WHERE email = $1", body.email)
    if not user or not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    return TokenResponse(access_token=_create_token(str(user["id"])))


@router.post("/refresh", response_model=TokenResponse)
async def refresh(user: CurrentUser):
    return TokenResponse(access_token=_create_token(str(user["id"])))


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return UserOut(
        id=str(user["id"]),
        email=user["email"],
        display_name=user["display_name"],
        role=user["role"],
        avatar_url=user.get("avatar_url"),
    )
