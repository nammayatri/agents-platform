import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agents.config.settings import settings
from agents.db.connection import close_db, init_db


def _configure_logging() -> None:
    """Set up logging for the entire application.

    Reads LOG_LEVEL from settings (env / .env).  Defaults to INFO.
    All ``agents.*`` loggers inherit this level.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,  # override any prior basicConfig (e.g. uvicorn)
    )

    # Ensure our loggers are at least at the configured level
    logging.getLogger("agents").setLevel(level)

    # Keep noisy third-party loggers quieter
    for noisy in ("httpcore", "httpx", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()


async def _seed_admin(pool) -> None:
    """Promote user to admin if ADMIN_SEED_EMAIL is configured."""
    email = settings.admin_seed_email
    if not email:
        return
    row = await pool.fetchrow("SELECT id, role FROM users WHERE email = $1", email)
    if row and row["role"] != "admin":
        await pool.execute("UPDATE users SET role = 'admin' WHERE id = $1", row["id"])
        import logging
        logging.getLogger(__name__).info("Promoted %s to admin (seed)", email)
    elif not row:
        import logging
        logging.getLogger(__name__).info(
            "ADMIN_SEED_EMAIL=%s not found yet — will promote on next restart after registration",
            email,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    pool = await init_db()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    app.state.db = pool
    app.state.redis = redis_client

    # Seed admin user if configured
    await _seed_admin(pool)

    # Start event-driven orchestrator in background
    from agents.orchestrator.events import EventBus
    from agents.orchestrator.loop import EventDrivenOrchestrator

    event_bus = EventBus(redis_client, worker_id=f"worker-{id(app)}")
    app.state.event_bus = event_bus

    orchestrator = EventDrivenOrchestrator(
        worker_id=f"worker-{id(app)}",
        db_pool=pool,
        redis=redis_client,
        event_bus=event_bus,
    )
    orchestrator_task = asyncio.create_task(orchestrator.run_forever())
    app.state.orchestrator = orchestrator

    yield

    # Shutdown
    orchestrator.shutdown_event.set()
    orchestrator_task.cancel()
    try:
        await orchestrator_task
    except asyncio.CancelledError:
        pass
    await redis_client.aclose()
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Orchestration Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # Vite dev server
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    from agents.api.routes import (
        admin,
        agents,
        auth,
        chat,
        deliverables,
        notifications,
        project_chat,
        projects,
        providers,
        skills,
        todos,
        websocket,
    )

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
    app.include_router(project_chat.router, prefix="/api", tags=["project-chat"])
    app.include_router(todos.router, prefix="/api", tags=["todos"])
    app.include_router(chat.router, prefix="/api", tags=["chat"])
    app.include_router(deliverables.router, prefix="/api", tags=["deliverables"])
    app.include_router(providers.router, prefix="/api/providers", tags=["providers"])
    app.include_router(skills.router, prefix="/api/config", tags=["skills-mcp"])
    app.include_router(agents.router, prefix="/api/config", tags=["agents"])
    app.include_router(notifications.router, prefix="/api", tags=["notifications"])
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
    app.include_router(websocket.router, tags=["websocket"])

    return app
