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
import os

import asyncpg
import redis.asyncio as aioredis

from agents.config.settings import settings
from agents.infra.notifier import Notifier
from agents.orchestrator.events import EventBus, TaskEvent
from agents.orchestrator.run_context import RunContext
from agents.orchestrator.scheduler import TaskScheduler
from agents.orchestrator.locks import LockManager
from agents.orchestrator.state_machine import transition_todo
from agents.orchestrator.workspace import WorkspaceManager
from agents.providers.registry import ProviderRegistry, get_registry

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
        self.provider_registry = get_registry(db_pool)
        self.notifier = Notifier(db_pool)
        self.workspace_mgr = WorkspaceManager(db_pool, settings.workspace_root)
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
                      WHERE state IN ('in_progress', 'testing')
                        AND id NOT IN (
                            SELECT todo_id FROM orchestrator_locks
                            WHERE expires_at > NOW()
                        )
                  )
            """)
            logger.info("Orphan sub_task recovery: %s", result)

            # Also clean up stale agent_runs stuck in 'running'
            stale_runs = await self.db.execute("""
                UPDATE agent_runs SET status = 'failed',
                       error_detail = 'Orphaned: worker died during execution',
                       completed_at = NOW()
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '30 minutes'
            """)
            logger.info("Orphan agent_run recovery: %s", stale_runs)
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
        if not todo or todo["state"] not in ("intake", "planning", "in_progress", "testing"):
            logger.info("Task %s not actionable (state=%s), releasing lock",
                        todo_id[:8], todo["state"] if todo else "NOT_FOUND")
            await self.locks.release(todo_id)
            await self.event_bus.ack(msg_id)
            return

        # Don't dispatch if awaiting user response, merge approval, or workspace edit review
        if todo.get("sub_state") in ("awaiting_response", "awaiting_merge_approval", "awaiting_release_approval", "awaiting_external_merge", "workspace_edited"):
            logger.info("Task %s awaiting user input (sub_state=%s), skipping", todo_id[:8], todo.get("sub_state"))
            await self.locks.release(todo_id)
            await self.event_bus.ack(msg_id)
            return

        # Skip if task already has a running pod (pod handles execution autonomously)
        if settings.k8s_spawn_pods and todo["state"] in ("in_progress", "testing"):
            has_pod = await self.db.fetchval(
                "SELECT 1 FROM task_pods WHERE todo_id = $1 AND state IN ('creating', 'running')",
                todo_id,
            )
            if has_pod:
                logger.debug("Task %s has active pod, skipping dispatch", todo_id[:8])
                await self.locks.release(todo_id)
                await self.event_bus.ack(msg_id)
                return

        # Dispatch — ack is deferred to _run_todo finally block
        task = asyncio.create_task(
            self._run_todo(dict(todo), msg_id=msg_id),
            name=f"todo-{todo_id[:8]}",
        )
        self.active_tasks[todo_id] = task
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

                # 8. Clean up terminated task pods
                if settings.k8s_spawn_pods:
                    await self._cleanup_terminated_pods()

                # 9. LRU-evict old task workspaces when disk usage exceeds 10GB
                try:
                    evicted = await self.workspace_mgr.evict_old_workspaces()
                    if evicted:
                        logger.info("Evicted %d old task workspaces", evicted)
                except Exception:
                    logger.debug("Workspace eviction failed", exc_info=True)

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
                LEFT JOIN task_pods tp ON tp.todo_id = t.id
                    AND tp.state IN ('creating', 'running')
                WHERE t.state IN ('intake', 'planning', 'in_progress', 'testing')
                  AND l.todo_id IS NULL
                  AND tp.todo_id IS NULL
                  AND (t.sub_state IS NULL OR t.sub_state NOT IN ('awaiting_response', 'awaiting_merge_approval', 'awaiting_release_approval', 'awaiting_external_merge', 'workspace_edited'))
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

    async def _run_todo(self, todo: dict, *, msg_id: str | None = None) -> None:
        """Process a single TODO through its lifecycle."""
        todo_id = str(todo["id"])
        logger.info("_run_todo START: %s (state=%s, sub_state=%s, title=%s)",
                     todo_id[:8], todo.get("state"), todo.get("sub_state"), todo.get("title"))
        try:
            # For in_progress/testing states: delegate to a task pod if spawning enabled
            if (
                settings.k8s_spawn_pods
                and todo["state"] in ("in_progress", "testing")
                and not settings.task_pod_mode  # don't recurse if we ARE a worker pod
            ):
                await self._ensure_task_pod(todo)
                return

            from agents.providers.mcp_executor import McpToolExecutor
            from agents.providers.tools_registry import ToolsRegistry

            ctx = RunContext(
                todo_id=todo_id,
                db=self.db,
                redis=self.redis,
                workspace_mgr=self.workspace_mgr,
                provider_registry=self.provider_registry,
                mcp_executor=McpToolExecutor(self.db),
                tools_registry=ToolsRegistry(self.db),
                notifier=self.notifier,
                event_bus=self.event_bus,
            )
            scheduler = TaskScheduler(todo_id=todo_id, ctx=ctx)
            await scheduler.run()
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
            if msg_id:
                try:
                    await self.event_bus.ack(msg_id)
                except Exception:
                    logger.warning("Failed to ack event %s for task %s", msg_id, todo_id[:8])
            await self.locks.release(todo_id)
            self.active_tasks.pop(todo_id, None)

    async def _ensure_task_pod(self, todo: dict) -> None:
        """Spawn or verify a task pod for in_progress execution.

        If a pod already exists and is running, this is a no-op.
        If no pod exists, creates PVC + Pod and records it in task_pods table.
        """
        from agents.infra.k8s_spawner import (
            create_pvc,
            create_task_pod,
            get_pod_record,
            get_pod_status,
            record_pod_created,
            update_pod_state,
        )
        from agents.utils.settings_helpers import parse_settings, read_setting

        todo_id = str(todo["id"])
        project_id = str(todo["project_id"])

        # Check if pod already exists in DB
        pod_record = await get_pod_record(self.db, todo_id)
        if pod_record and pod_record["state"] in ("creating", "running"):
            # Pod exists — verify it's still alive
            status = await get_pod_status(pod_record["pod_name"])
            if status["phase"] in ("Pending", "Running"):
                # Pod is alive, update IP if available
                if status.get("pod_ip") and not pod_record.get("pod_ip"):
                    await update_pod_state(
                        self.db, todo_id, "running", pod_ip=status["pod_ip"]
                    )
                logger.debug("Task pod for %s already running", todo_id[:8])
                return
            elif status["phase"] in ("Failed", "Unknown"):
                logger.warning("Task pod for %s in state %s, will recreate",
                               todo_id[:8], status["phase"])
                await update_pod_state(
                    self.db, todo_id, "failed",
                    error_message=f"Pod phase: {status['phase']}",
                )
            # If Succeeded or NotFound, fall through to recreate

        # Load project settings for worker config
        project = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", project_id
        )
        proj_settings = parse_settings((project or {}).get("settings_json"))

        # Resolve worker settings from project.worker section
        worker_image = read_setting(proj_settings, "worker.image", None, "")
        pvc_size_gb = read_setting(proj_settings, "worker.pvc_size_gb", None, 20)
        boot_script = read_setting(proj_settings, "worker.boot_script", None, "")
        resources = read_setting(proj_settings, "worker.resources", None, None)
        node_type = read_setting(proj_settings, "worker.node_type", None, "")
        pod_spec_override = read_setting(proj_settings, "worker.pod_spec_override", None, None)

        if not worker_image:
            worker_image = settings.k8s_worker_image or os.environ.get("BACKEND_IMAGE", "agents-backend:latest")

        if isinstance(pvc_size_gb, str):
            pvc_size_gb = int(pvc_size_gb)

        namespace = settings.k8s_namespace

        logger.info(
            "Spawning task pod for %s: image=%s pvc=%dGi boot_script=%s node_type=%s advanced=%s",
            todo_id[:8], worker_image, pvc_size_gb, bool(boot_script),
            node_type or "any", bool(pod_spec_override),
        )

        # Create PVC
        pvc_name = await create_pvc(todo_id, size_gb=pvc_size_gb, namespace=namespace)

        # Create Pod
        pod_name = await create_task_pod(
            todo_id,
            project_id,
            image=worker_image,
            pvc_name=pvc_name,
            boot_script=boot_script or None,
            namespace=namespace,
            resources=resources,
            node_type=node_type or None,
            pod_spec_override=pod_spec_override,
        )

        # Record in DB
        await record_pod_created(
            self.db,
            todo_id=todo_id,
            project_id=project_id,
            pod_name=pod_name,
            pvc_name=pvc_name,
            image=worker_image,
            pvc_size_gb=pvc_size_gb,
            boot_script=boot_script or None,
            namespace=namespace,
        )

        logger.info("Task pod %s created for %s", pod_name, todo_id[:8])

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
            WHERE t.state IN ('in_progress', 'testing')
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

    async def _cleanup_terminated_pods(self) -> None:
        """Clean up pods whose tasks have reached terminal state, and detect dead pods."""
        try:
            from agents.infra.k8s_spawner import (
                cleanup_task_resources,
                delete_task_pvc,
                get_pod_status,
                update_pod_state,
            )

            # 1. Find pods in creating/running state whose tasks are done
            rows = await self.db.fetch(
                """
                SELECT tp.todo_id, tp.pod_name, tp.namespace
                FROM task_pods tp
                JOIN todo_items t ON t.id = tp.todo_id
                WHERE tp.state IN ('creating', 'running')
                  AND t.state IN ('completed', 'cancelled', 'failed', 'review')
                """,
            )
            for row in rows:
                todo_id = str(row["todo_id"])
                logger.info("Cleaning up task pod for %s (task terminated)", todo_id[:8])
                try:
                    await cleanup_task_resources(todo_id, row["namespace"])
                    await update_pod_state(self.db, todo_id, "terminated")
                except Exception as e:
                    logger.warning("Failed to cleanup pod for %s: %s", todo_id[:8], e)
                    await update_pod_state(
                        self.db, todo_id, "failed",
                        error_message=f"Cleanup failed: {e}",
                    )

            # 2. Detect dead pods whose tasks are still in_progress — mark as failed
            #    so the fallback poll can re-spawn them
            active_pods = await self.db.fetch(
                """
                SELECT tp.todo_id, tp.pod_name, tp.namespace, tp.created_at
                FROM task_pods tp
                JOIN todo_items t ON t.id = tp.todo_id
                WHERE tp.state IN ('creating', 'running')
                  AND t.state IN ('in_progress', 'testing')
                """,
            )
            for row in active_pods:
                todo_id = str(row["todo_id"])
                try:
                    status = await get_pod_status(row["pod_name"], row["namespace"])
                    if status["phase"] in ("Failed", "Unknown"):
                        logger.warning(
                            "Task pod for %s is dead (phase=%s), marking failed for re-spawn",
                            todo_id[:8], status["phase"],
                        )
                        await update_pod_state(
                            self.db, todo_id, "failed",
                            error_message=f"Pod died: {status['phase']}",
                        )
                    elif status["phase"] == "NotFound":
                        # Pod disappeared — mark failed so fallback poll re-creates it
                        await update_pod_state(
                            self.db, todo_id, "failed",
                            error_message="Pod not found (node eviction or crash)",
                        )
                except Exception:
                    pass  # K8s API flake — will retry next health check

            # 3. Clean up pods marked 'terminated' for >1h (PVC deletion)
            old_rows = await self.db.fetch(
                """
                SELECT todo_id, namespace FROM task_pods
                WHERE state = 'terminated'
                  AND stopped_at < NOW() - INTERVAL '1 hour'
                """,
            )
            for row in old_rows:
                todo_id = str(row["todo_id"])
                try:
                    await delete_task_pvc(todo_id, row["namespace"])
                    await self.db.execute(
                        "DELETE FROM task_pods WHERE todo_id = $1", todo_id
                    )
                except Exception:
                    pass  # Best effort

        except Exception:
            logger.debug("Pod cleanup check failed", exc_info=True)
