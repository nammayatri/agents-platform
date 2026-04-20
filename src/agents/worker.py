"""Task worker entrypoint — runs inside per-task pods.

Each pod handles exactly one task. On startup:
1. Executes optional boot_script (from BOOT_SCRIPT env or project settings)
2. Starts a minimal FastAPI server for workspace API proxying
3. Runs the TaskScheduler loop for the assigned todo_id
4. Publishes pod IP to DB for API routing
5. On task completion (terminal state), exits cleanly

Usage:
    python -m agents.worker --todo-id <uuid>
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys

import asyncpg
import redis.asyncio as aioredis
import uvicorn

from agents.config.settings import settings


def _configure_logging():
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("agents").setLevel(level)
    for noisy in ("httpcore", "httpx", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger(__name__)


# ── Boot Script Execution ────────────────────────────────────────────


def run_boot_script(script: str, workspace_root: str) -> None:
    """Execute the boot script in a subprocess.

    The script runs with cwd=workspace_root and has access to all env vars.
    Failures here are logged but don't prevent the worker from starting —
    the task will still attempt execution.
    """
    if not script or not script.strip():
        return

    logger.info("Running boot script (%d chars)...", len(script))
    os.makedirs(workspace_root, exist_ok=True)

    try:
        result = subprocess.run(
            ["/bin/sh", "-c", script],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max for boot script
        )
        if result.stdout:
            logger.info("Boot script stdout:\n%s", result.stdout[-2000:])
        if result.returncode != 0:
            logger.error(
                "Boot script failed (exit %d):\n%s",
                result.returncode, result.stderr[-2000:],
            )
        else:
            logger.info("Boot script completed successfully")
    except subprocess.TimeoutExpired:
        logger.error("Boot script timed out (300s limit)")
    except Exception as e:
        logger.error("Boot script error: %s", e)


# ── Minimal Worker API ───────────────────────────────────────────────


def create_worker_app(todo_id: str, db_pool: asyncpg.Pool, redis_client):
    """Create a minimal FastAPI app for the worker pod.

    Exposes:
    - /healthz — liveness/readiness probe
    - /api/todos/{todo_id}/workspace/* — workspace file operations (same as backend)
    """
    from fastapi import FastAPI

    app = FastAPI(title="Task Worker", version="0.1.0")

    app.state.db = db_pool
    app.state.redis = redis_client
    app.state.todo_id = todo_id

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "todo_id": todo_id}

    # Mount workspace routes — they read files from local PVC
    from agents.api.routes import workspace
    app.include_router(workspace.router, prefix="/api", tags=["workspace"])

    return app


# ── Task Execution Loop ──────────────────────────────────────────────


async def run_task_loop(todo_id: str, db: asyncpg.Pool, redis_client) -> None:
    """Run the scheduler loop for this task until terminal state."""
    from agents.infra.notifier import Notifier
    from agents.orchestrator.events import EventBus
    from agents.orchestrator.run_context import RunContext
    from agents.orchestrator.scheduler import TaskScheduler
    from agents.orchestrator.workspace import WorkspaceManager
    from agents.providers.mcp_executor import McpToolExecutor
    from agents.providers.registry import get_registry
    from agents.providers.tools_registry import ToolsRegistry
    from agents.infra.k8s_spawner import update_pod_state

    worker_id = f"task-worker-{todo_id[:8]}"
    workspace_mgr = WorkspaceManager(db, settings.workspace_root)
    event_bus = EventBus(redis_client, worker_id=worker_id)
    notifier = Notifier(db)
    provider_registry = get_registry(db)
    mcp_executor = McpToolExecutor(db)
    tools_registry = ToolsRegistry(db)

    # Report pod as running
    pod_ip = os.environ.get("POD_IP", "")
    await update_pod_state(db, todo_id, "running", pod_ip=pod_ip)
    logger.info("Task worker running: todo=%s pod_ip=%s", todo_id[:8], pod_ip)

    terminal_states = ("completed", "cancelled", "failed", "review")
    poll_interval = 5  # seconds between checks

    while True:
        todo = await db.fetchrow(
            "SELECT state, sub_state FROM todo_items WHERE id = $1", todo_id
        )
        if not todo:
            logger.error("Task %s not found in DB, exiting", todo_id[:8])
            break

        state = todo["state"]
        sub_state = todo.get("sub_state")

        if state in terminal_states:
            logger.info("Task %s reached terminal state: %s", todo_id[:8], state)
            break

        # Skip if awaiting user input
        if sub_state in (
            "awaiting_response", "awaiting_merge_approval",
            "awaiting_release_approval", "awaiting_external_merge", "workspace_edited",
        ):
            await asyncio.sleep(poll_interval)
            continue

        # Run one scheduling pass
        try:
            ctx = RunContext(
                todo_id=todo_id,
                db=db,
                redis=redis_client,
                workspace_mgr=workspace_mgr,
                provider_registry=provider_registry,
                mcp_executor=mcp_executor,
                tools_registry=tools_registry,
                notifier=notifier,
                event_bus=event_bus,
            )
            scheduler = TaskScheduler(todo_id=todo_id, ctx=ctx)
            await scheduler.run()
        except Exception as e:
            logger.exception("Scheduler pass failed: %s", e)

        await asyncio.sleep(poll_interval)

    # Mark pod as terminated
    await update_pod_state(db, todo_id, "terminated")
    logger.info("Task worker exiting: todo=%s", todo_id[:8])


# ── Main ─────────────────────────────────────────────────────────────


async def _main(todo_id: str) -> None:
    """Async main: boot script → start API → run task loop."""
    workspace_root = os.environ.get("WORKSPACE_ROOT", settings.workspace_root)
    # Override settings for this pod
    settings.workspace_root = workspace_root

    # Run boot script
    boot_script = os.environ.get("BOOT_SCRIPT", "")
    if boot_script:
        run_boot_script(boot_script, workspace_root)

    # Connect to shared DB + Redis
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    db = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    redis_client = aioredis.from_url(
        os.environ.get("REDIS_URL", settings.redis_url),
        decode_responses=True,
    )

    # Start minimal API server in background
    app = create_worker_app(todo_id, db, redis_client)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=8000,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    try:
        await run_task_loop(todo_id, db, redis_client)
    finally:
        # Graceful shutdown
        server.should_exit = True
        api_task.cancel()
        try:
            await api_task
        except asyncio.CancelledError:
            pass
        await redis_client.aclose()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Task worker pod entrypoint")
    parser.add_argument("--todo-id", required=True, help="UUID of the task to execute")
    args = parser.parse_args()

    logger.info("Starting task worker for todo_id=%s", args.todo_id)
    asyncio.run(_main(args.todo_id))


if __name__ == "__main__":
    main()
