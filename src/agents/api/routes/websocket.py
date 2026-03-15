import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/todos/{todo_id}")
async def task_websocket(websocket: WebSocket, todo_id: str):
    """WebSocket endpoint for real-time task updates.

    Subscribes to Redis pub/sub for:
    - task:{todo_id}:events (state changes, chat messages, deliverables)
    - task:{todo_id}:progress (agent progress updates)
    """
    await websocket.accept()

    redis = websocket.app.state.redis
    pubsub = redis.pubsub()
    reader_task = None

    try:
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

        # Keep connection alive with pings
        while True:
            try:
                # Wait for client messages (pongs, or user might send via WS too)
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping to keep alive
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
