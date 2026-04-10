import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/todos/{todo_id}")
async def task_websocket(websocket: WebSocket, todo_id: str):
    """WebSocket endpoint for real-time task updates.

    Uses the **snapshot + stream** pattern:
    1. On connect, sends current task state + active subtask progress.
    2. Then subscribes to Redis pub/sub for live updates.

    Channels: task:{todo_id}:events, task:{todo_id}:progress
    """
    await websocket.accept()

    redis = websocket.app.state.redis
    db = websocket.app.state.db
    pubsub = redis.pubsub()
    reader_task = None

    try:
        # ── Snapshot: send current state on connect ──
        todo = await db.fetchrow(
            "SELECT state, sub_state, error_message FROM todo_items WHERE id = $1",
            todo_id,
        )
        if todo:
            snapshot: dict = {"type": "state_change", "state": todo["state"]}
            if todo.get("sub_state"):
                snapshot["sub_state"] = todo["sub_state"]
            if todo.get("error_message"):
                snapshot["error_message"] = todo["error_message"]
            await websocket.send_text(json.dumps(snapshot))

            # Send progress for running subtasks
            running = await db.fetch(
                "SELECT id, progress_pct, progress_message FROM sub_tasks "
                "WHERE todo_id = $1 AND status = 'running'",
                todo_id,
            )
            for st in running:
                if st.get("progress_message"):
                    await websocket.send_text(json.dumps({
                        "type": "activity",
                        "sub_task_id": str(st["id"]),
                        "activity": st["progress_message"],
                    }))

        # ── Stream: subscribe to live updates ──
        await pubsub.subscribe(f"task:{todo_id}:events", f"task:{todo_id}:progress")

        async def reader():
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await websocket.send_text(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("WebSocket reader stopped for todo %s", todo_id)

        reader_task = asyncio.create_task(reader())

        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    finally:
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await pubsub.unsubscribe()
        await pubsub.aclose()


@router.websocket("/ws/chat/sessions/{session_id}")
async def chat_session_websocket(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time project chat activity updates.

    Subscribes to Redis pub/sub for:
    - chat:session:{session_id}:activity (tool call activity during LLM processing)
    """
    await websocket.accept()

    redis = websocket.app.state.redis
    pubsub = redis.pubsub()
    reader_task = None

    try:
        await pubsub.subscribe(f"chat:session:{session_id}:activity")

        async def reader():
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await websocket.send_text(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("WebSocket reader stopped for chat session %s", session_id)

        reader_task = asyncio.create_task(reader())

        # Keep connection alive with pings
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    finally:
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await pubsub.unsubscribe()
        await pubsub.aclose()


@router.websocket("/ws/projects/{project_id}/analysis")
async def project_analysis_websocket(websocket: WebSocket, project_id: str):
    """WebSocket endpoint for real-time project analysis progress.

    Uses the **snapshot + stream** pattern:
    1. On connect, reads the current analysis state from DB and sends it
       as the first message (so the client is never stale, even on refresh).
    2. Then subscribes to Redis pub/sub for live incremental updates.

    Channel: project:{project_id}:analysis
    """
    await websocket.accept()

    redis = websocket.app.state.redis
    db = websocket.app.state.db
    pubsub = redis.pubsub()
    reader_task = None

    try:
        # ── Snapshot: send current state immediately on connect ──
        row = await db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", project_id,
        )
        if row:
            settings = row["settings_json"] or {}
            if isinstance(settings, str):
                settings = json.loads(settings)
            step = settings.get("analysis_step")
            if step:
                await websocket.send_text(json.dumps({
                    "step": step,
                    "detail": settings.get("analysis_detail", ""),
                }))

        # ── Stream: subscribe to live updates ──
        await pubsub.subscribe(f"project:{project_id}:analysis")

        async def reader():
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await websocket.send_text(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("WebSocket reader stopped for project analysis %s", project_id)

        reader_task = asyncio.create_task(reader())

        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    finally:
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await pubsub.unsubscribe()
        await pubsub.aclose()


@router.websocket("/ws/projects/{project_id}/pipeline")
async def pipeline_websocket(websocket: WebSocket, project_id: str):
    """WebSocket endpoint for real-time merge pipeline updates.

    Uses the **snapshot + stream** pattern:
    1. On connect, sends the current status of all active pipeline runs.
    2. Then subscribes to Redis pub/sub for live updates.

    Channel: pipeline:{project_id}:events
    """
    await websocket.accept()

    redis = websocket.app.state.redis
    db = websocket.app.state.db
    pubsub = redis.pubsub()
    reader_task = None

    try:
        # ── Snapshot: send active run statuses on connect ──
        active_runs = await db.fetch(
            "SELECT id, status, pr_number, branch_name FROM pipeline_runs "
            "WHERE project_id = $1 AND status IN ('pending', 'testing', 'deploying') "
            "ORDER BY created_at DESC",
            project_id,
        )
        for run in active_runs:
            await websocket.send_text(json.dumps({
                "type": "pipeline_status",
                "run_id": str(run["id"]),
                "status": run["status"],
                "pr_number": run["pr_number"],
                "branch_name": run.get("branch_name", ""),
            }))

        # ── Stream: subscribe to live updates ──
        await pubsub.subscribe(f"pipeline:{project_id}:events")

        async def reader():
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await websocket.send_text(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("WebSocket reader stopped for pipeline %s", project_id)

        reader_task = asyncio.create_task(reader())

        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    finally:
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await pubsub.unsubscribe()
        await pubsub.aclose()
