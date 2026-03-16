"""Event-driven orchestrator.

Replaces the polling-based OrchestratorLoop with an event-driven architecture.
Uses Redis Streams for durable event delivery with consumer groups.

Two concurrent loops:
1. Event consumer loop (primary): reads task events from Redis Stream, dispatches
   AgentCoordinator instances immediately when events arrive.
2. Health check loop (every 60s): heartbeat, lock reclamation, stuck detection,
   stale event reclamation, scheduled task activation, and a fallback poll as
   a safety net for any events that were missed.
"""

import asyncio
import logging

import asyncpg
import redis.asyncio as aioredis

from agents.config.settings import settings
from agents.infra.notifier import Notifier
from agents.orchestrator.coordinator import AgentCoordinator
from agents.orchestrator.events import EventBus, TaskEvent
from agents.orchestrator.locks import LockManager
from agents.orchestrator.state_machine import transition_todo
from agents.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class EventDrivenOrchestrator:
    def __init__(
        self,
        worker_id: str,
        db_pool: asyncpg.Pool,
        redis: aioredis.Redis,
        event_bus: EventBus,
    ):
        self.worker_id = worker_id
        self.db = db_pool
        self.redis = redis
        self.event_bus = event_bus
        self.locks = LockManager(db_pool, worker_id, settings.orchestrator_lock_ttl)
        self.provider_registry = ProviderRegistry(db_pool)
        self.notifier = Notifier(db_pool)
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.shutdown_event = asyncio.Event()
        self.max_concurrent = settings.orchestrator_max_concurrent
        self.health_check_interval = settings.orchestrator_health_check_interval

    async def run_forever(self) -> None:
        """Main entry point. Runs event consumer + health check concurrently."""
        await self.event_bus.ensure_consumer_group()
        await self._recover_orphaned_tasks()
        logger.info(
            "EventDrivenOrchestrator %s starting (max_concurrent=%d, health_check=%ds)",
            self.worker_id, self.max_concurrent, self.health_check_interval,
        )

        event_loop = asyncio.create_task(
            self._event_consumer_loop(), name="event-consumer",
        )
        health_loop = asyncio.create_task(
            self._health_check_loop(), name="health-check",
        )

        try:
            done, pending = await asyncio.wait(
                [event_loop, health_loop],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            # If one loop died unexpectedly, cancel the other
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    logger.error("Loop died: %s", task.exception())
        except asyncio.CancelledError:
            event_loop.cancel()
            health_loop.cancel()

        # Graceful shutdown: drain active tasks before force-cancelling
        if self.active_tasks:
            logger.info("Draining %d active tasks (30s grace)...", len(self.active_tasks))
            _, still_running = await asyncio.wait(
                self.active_tasks.values(), timeout=30,
            )
            if still_running:
                logger.warning("Force-cancelling %d tasks", len(still_running))
                for task in still_running:
                    task.cancel()
                await asyncio.gather(*still_running, return_exceptions=True)

        await self.notifier.close()
        logger.info("EventDrivenOrchestrator %s stopped", self.worker_id)

    # ── Startup Recovery ────────────────────────────────────────────

    async def _recover_orphaned_tasks(self) -> None:
        """On startup, reset sub-tasks left in running/assigned by dead workers.

        Targets sub-tasks whose parent todo is in_progress but has no valid
        (non-expired) lock — meaning the previous worker died.
        """
        try:
            result = await self.db.execute("""
                UPDATE sub_tasks SET status = 'pending', progress_pct = 0,
                       progress_message = 'Reset after worker restart'
                WHERE status IN ('assigned', 'running')
                  AND todo_id IN (
                      SELECT id FROM todo_items
                      WHERE state = 'in_progress'
                        AND id NOT IN (
                            SELECT todo_id FROM orchestrator_locks
                            WHERE expires_at > NOW()
                        )
                  )
            """)
            logger.info("Orphan recovery: %s", result)
        except Exception:
            logger.warning("Orphan recovery failed", exc_info=True)

    # ── Event Consumer Loop ──────────────────────────────────────────

    async def _event_consumer_loop(self) -> None:
        """Primary loop: consume events from Redis Stream."""
        while not self.shutdown_event.is_set():
            try:
                events = await self.event_bus.consume(count=10, block_ms=5000)
                for msg_id, event in events:
                    await self._handle_event(msg_id, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event consumer error")
                await asyncio.sleep(1)

    async def _handle_event(self, msg_id: str, event: TaskEvent) -> None:
        """Process a single event: lock + dispatch coordinator."""
        todo_id = event.todo_id
        logger.info("_handle_event: todo=%s event=%s state=%s",
                     todo_id[:8], event.event_type, event.state)

        # Skip terminal state events — nothing to orchestrate
        if event.state in ("completed", "cancelled", "failed", "scheduled"):
            logger.debug("Skipping terminal event for %s (state=%s)", todo_id[:8], event.state)
            await self.event_bus.ack(msg_id)
            return

        # Already processing this task
        if todo_id in self.active_tasks:
            logger.debug("Task %s already in active_tasks, acking", todo_id[:8])
            await self.event_bus.ack(msg_id)
            return

        # At capacity — don't ack, will be retried
        if len(self.active_tasks) >= self.max_concurrent:
            logger.warning("At max capacity (%d/%d), deferring event for %s",
                           len(self.active_tasks), self.max_concurrent, todo_id[:8])
            return

        # Try to acquire lock
        locked = await self.locks.try_lock(todo_id)
        if not locked:
            # Another worker has it
            logger.debug("Could not acquire lock for %s, another worker has it", todo_id[:8])
            await self.event_bus.ack(msg_id)
            return

        # Verify the task is still actionable (DB is source of truth)
        todo = await self.db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
        if not todo or todo["state"] not in ("intake", "planning", "in_progress"):
            logger.info("Task %s not actionable (state=%s), releasing lock",
                        todo_id[:8], todo["state"] if todo else "NOT_FOUND")
            await self.locks.release(todo_id)
            await self.event_bus.ack(msg_id)
            return

        # Don't dispatch if awaiting user response
        if todo.get("sub_state") == "awaiting_response":
            logger.info("Task %s awaiting user response, skipping", todo_id[:8])
            await self.locks.release(todo_id)
            await self.event_bus.ack(msg_id)
            return

        # Dispatch
        task = asyncio.create_task(
            self._run_todo(dict(todo)),
            name=f"todo-{todo_id[:8]}",
        )
        self.active_tasks[todo_id] = task
        await self.event_bus.ack(msg_id)
        logger.info("Dispatched task %s (state=%s, sub_state=%s) from event %s",
                     todo_id[:8], todo["state"], todo.get("sub_state"), event.event_type)

    # ── Health Check Loop ────────────────────────────────────────────

    async def _health_check_loop(self) -> None:
        """Secondary loop: periodic maintenance tasks."""
        while not self.shutdown_event.is_set():
            try:
                # 1. Heartbeat: extend locks we own
                await self.locks.heartbeat()

                # 2. Reap finished asyncio tasks
                await self._reap_finished()

                # 3. Reclaim expired locks from dead workers
                reclaimed = await self.locks.reclaim_expired()
                if reclaimed:
                    logger.info("Reclaimed %d expired locks", reclaimed)

                # 4. Stuck task detection + notification
                await self._check_stuck_tasks()

                # 5. Reclaim stale events from crashed consumers
                await self._reclaim_stale_events()

                # 6. Activate scheduled tasks whose time has come
                await self._activate_scheduled_tasks()

                # 7. Fallback poll: catch anything events missed
                await self._fallback_poll()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Health check error")

            await asyncio.sleep(self.health_check_interval)

    async def _reclaim_stale_events(self) -> None:
        """Reclaim events from consumers that crashed."""
        try:
            stale = await self.event_bus.reclaim_stale(min_idle_ms=60_000)
            for msg_id, event in stale:
                await self._handle_event(msg_id, event)
        except Exception:
            logger.warning("Failed to reclaim stale events", exc_info=True)

    async def _activate_scheduled_tasks(self) -> None:
        """Activate tasks whose scheduled_at time has arrived."""
        try:
            due_tasks = await self.db.fetch(
                """
                SELECT id, title FROM todo_items
                WHERE state = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= NOW()
                LIMIT 20
                """,
            )
            for task in due_tasks:
                todo_id = str(task["id"])
                result = await transition_todo(
                    self.db, todo_id, "intake",
                    event_bus=self.event_bus,
                    redis=self.redis,
                )
                if result:
                    logger.info("Scheduled task %s activated (was due)", todo_id[:8])
        except Exception:
            logger.warning("Failed to activate scheduled tasks", exc_info=True)

    async def _fallback_poll(self) -> None:
        """Safety net: find actionable tasks that may have missed events."""
        if len(self.active_tasks) >= self.max_concurrent:
            return

        available = self.max_concurrent - len(self.active_tasks)
        try:
            rows = await self.db.fetch(
                """
                SELECT t.* FROM todo_items t
                LEFT JOIN orchestrator_locks l ON t.id = l.todo_id
                    AND l.expires_at > NOW()
                WHERE t.state IN ('intake', 'planning', 'in_progress')
                  AND l.todo_id IS NULL
                  AND (t.sub_state IS DISTINCT FROM 'awaiting_response')
                ORDER BY
                    CASE t.priority
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                    END,
                    t.created_at ASC
                LIMIT $1
                """,
                available,
            )
        except Exception:
            logger.warning("Fallback poll query failed", exc_info=True)
            return

        for todo in rows:
            todo_id = str(todo["id"])
            if todo_id in self.active_tasks:
                continue
            locked = await self.locks.try_lock(todo_id)
            if not locked:
                continue
            task = asyncio.create_task(
                self._run_todo(dict(todo)),
                name=f"todo-{todo_id[:8]}",
            )
            self.active_tasks[todo_id] = task
            logger.info("Fallback poll dispatched task %s (state=%s)", todo_id[:8], todo["state"])

    # ── Task Execution ───────────────────────────────────────────────

    async def _run_todo(self, todo: dict) -> None:
        """Process a single TODO through its lifecycle."""
        todo_id = str(todo["id"])
        logger.info("_run_todo START: %s (state=%s, sub_state=%s, title=%s)",
                     todo_id[:8], todo.get("state"), todo.get("sub_state"), todo.get("title"))
        try:
            coordinator = AgentCoordinator(
                todo_id=todo_id,
                db=self.db,
                redis=self.redis,
                provider_registry=self.provider_registry,
                notifier=self.notifier,
            )
            await coordinator.run()
            # Log final state after run
            final = await self.db.fetchrow(
                "SELECT state, sub_state FROM todo_items WHERE id = $1", todo_id
            )
            if final:
                logger.info("_run_todo DONE: %s → state=%s sub_state=%s",
                            todo_id[:8], final["state"], final.get("sub_state"))
        except Exception as e:
            logger.exception("Task %s failed: %s", todo_id[:8], e)
            error_msg = f"{type(e).__name__}: {e}"
            if len(error_msg) > 1500:
                error_msg = error_msg[:1500] + "..."
            try:
                await transition_todo(
                    self.db, todo_id, "failed", error_message=error_msg,
                    redis=self.redis,
                )
                todo_data = await self.db.fetchrow(
                    "SELECT title, creator_id FROM todo_items WHERE id = $1", todo_id,
                )
                if todo_data:
                    await self.notifier.notify(
                        str(todo_data["creator_id"]),
                        "failed",
                        {
                            "todo_id": todo_id,
                            "title": todo_data["title"],
                            "detail": error_msg,
                        },
                    )
            except Exception:
                logger.exception("Failed to transition task %s to failed", todo_id[:8])
        finally:
            await self.locks.release(todo_id)
            self.active_tasks.pop(todo_id, None)

    async def _reap_finished(self) -> None:
        """Remove completed/cancelled asyncio.Tasks from tracking."""
        done = [tid for tid, task in self.active_tasks.items() if task.done()]
        for tid in done:
            task = self.active_tasks.pop(tid)
            exc = task.exception() if not task.cancelled() else None
            if exc:
                logger.error("Task %s raised: %s", tid[:8], exc)

    async def _check_stuck_tasks(self) -> None:
        """Detect tasks stuck for >30 min and notify humans."""
        stuck_rows = await self.db.fetch(
            """
            SELECT t.id, t.title, t.creator_id, t.sub_state, t.updated_at
            FROM todo_items t
            WHERE t.state = 'in_progress'
              AND t.updated_at < NOW() - INTERVAL '30 minutes'
              AND (t.stuck_notified_at IS NULL
                   OR t.stuck_notified_at < NOW() - INTERVAL '2 hours')
            """,
        )
        for row in stuck_rows:
            await self.notifier.notify(
                str(row["creator_id"]),
                "stuck",
                {
                    "todo_id": str(row["id"]),
                    "title": row["title"],
                    "detail": (
                        f"Sub-state: {row['sub_state'] or 'unknown'}. "
                        f"Last updated: {row['updated_at'].isoformat()}"
                    ),
                },
            )
            await self.db.execute(
                "UPDATE todo_items SET stuck_notified_at = NOW() WHERE id = $1",
                row["id"],
            )
