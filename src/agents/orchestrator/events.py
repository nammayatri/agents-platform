"""Event bus for the orchestrator, backed by Redis Streams.

Redis Streams provide durable, replay-safe event delivery with consumer groups.
Unlike pub/sub (fire-and-forget), missed events can be reclaimed and reprocessed.

Usage:
    bus = EventBus(redis, "worker-1")
    await bus.ensure_consumer_group()
    await bus.publish(TaskEvent(event_type="task_created", todo_id="abc", state="intake"))
    events = await bus.consume(count=10, block_ms=5000)
    for msg_id, event in events:
        ... process event ...
        await bus.ack(msg_id)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

STREAM_KEY = "orchestrator:task_events"
GROUP_NAME = "orchestrator-workers"
MAX_STREAM_LEN = 10_000


@dataclass
class TaskEvent:
    event_type: str  # task_created | task_activated | state_changed | user_replied | scheduled_due
    todo_id: str
    state: str
    sub_state: str | None = None
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, str]:
        """Flatten to string values for Redis XADD."""
        return {
            "event_type": self.event_type,
            "todo_id": self.todo_id,
            "state": self.state,
            "sub_state": self.sub_state or "",
            "timestamp": self.timestamp,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> TaskEvent:
        meta = data.get("metadata", "{}")
        try:
            metadata = json.loads(meta) if meta else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return cls(
            event_type=data.get("event_type", "unknown"),
            todo_id=data.get("todo_id", ""),
            state=data.get("state", ""),
            sub_state=data.get("sub_state") or None,
            timestamp=data.get("timestamp", ""),
            metadata=metadata,
        )


class EventBus:
    """Redis Streams-backed event bus with consumer group support."""

    def __init__(self, redis: aioredis.Redis, worker_id: str):
        self.redis = redis
        self.worker_id = worker_id

    async def ensure_consumer_group(self) -> None:
        """Create the consumer group if it doesn't exist."""
        try:
            await self.redis.xgroup_create(
                STREAM_KEY, GROUP_NAME, id="0", mkstream=True,
            )
            logger.info("Created consumer group %s on stream %s", GROUP_NAME, STREAM_KEY)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            # Group already exists — fine

    async def publish(self, event: TaskEvent) -> str:
        """Publish an event to the stream. Returns the message ID."""
        msg_id = await self.redis.xadd(
            STREAM_KEY,
            event.to_dict(),
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )
        logger.debug("Published %s for task %s (msg=%s)", event.event_type, event.todo_id[:8], msg_id)
        return msg_id

    async def consume(
        self, count: int = 10, block_ms: int = 5000,
    ) -> list[tuple[str, TaskEvent]]:
        """Read pending events for this worker via XREADGROUP.

        Blocks up to `block_ms` milliseconds. Returns list of (message_id, TaskEvent).
        """
        results = await self.redis.xreadgroup(
            groupname=GROUP_NAME,
            consumername=self.worker_id,
            streams={STREAM_KEY: ">"},
            count=count,
            block=block_ms,
        )
        if not results:
            return []

        events = []
        for _stream_name, messages in results:
            for msg_id, data in messages:
                events.append((msg_id, TaskEvent.from_dict(data)))
        return events

    async def ack(self, message_id: str) -> None:
        """Acknowledge a processed event."""
        await self.redis.xack(STREAM_KEY, GROUP_NAME, message_id)

    async def reclaim_stale(self, min_idle_ms: int = 60_000, count: int = 20) -> list[tuple[str, TaskEvent]]:
        """Reclaim events from consumers that crashed (idle > min_idle_ms).

        Uses XPENDING to find stale messages, then XCLAIM to take ownership.
        """
        try:
            pending = await self.redis.xpending_range(
                STREAM_KEY, GROUP_NAME, "-", "+", count=count,
            )
        except aioredis.ResponseError:
            return []

        if not pending:
            return []

        stale_ids = []
        for entry in pending:
            idle_ms = entry.get("time_since_delivered", 0)
            if idle_ms >= min_idle_ms:
                stale_ids.append(entry["message_id"])

        if not stale_ids:
            return []

        claimed = await self.redis.xclaim(
            STREAM_KEY, GROUP_NAME, self.worker_id,
            min_idle_time=min_idle_ms,
            message_ids=stale_ids,
        )

        events = []
        for msg_id, data in claimed:
            if data:  # XCLAIM can return None for deleted messages
                events.append((msg_id, TaskEvent.from_dict(data)))
        logger.info("Reclaimed %d stale events from stream", len(events))
        return events
