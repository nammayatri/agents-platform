"""WebSocket utilities for real-time updates."""

import asyncio
import json

import websockets

from agents_cli import config


async def listen_task_events(
    todo_id: str,
    on_state_change=None,
    on_progress=None,
    on_activity=None,
    stop_event: asyncio.Event | None = None,
):
    """Connect to task WebSocket and dispatch events."""
    api_url = config.get_api_url()
    ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/todos/{todo_id}"

    stop = stop_event or asyncio.Event()

    try:
        async with websockets.connect(ws_url) as ws:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(raw)
                    event_type = data.get("type")

                    if event_type == "state_change" and on_state_change:
                        on_state_change(data.get("state", ""))
                    elif event_type == "progress" and on_progress:
                        on_progress(
                            data.get("sub_task_id", ""),
                            data.get("progress_pct", 0),
                            data.get("message", ""),
                        )
                    elif event_type == "activity" and on_activity:
                        on_activity(
                            data.get("sub_task_id", ""),
                            data.get("activity", ""),
                        )
                    elif event_type == "ping":
                        continue

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
    except Exception:
        pass
