"""Lock management for the orchestrator loop.

Uses PostgreSQL advisory-style locks via the orchestrator_locks table.
Ensures exactly one worker processes a given TODO at any time.
"""

import asyncpg

from agents.utils.db_retry import db_retry


class LockManager:
    def __init__(self, db: asyncpg.Pool, worker_id: str, ttl_seconds: int = 300):
        self.db = db
        self.worker_id = worker_id
        self.ttl_seconds = ttl_seconds

    async def try_lock(self, todo_id: str) -> bool:
        """Attempt to acquire a lock. Returns True if acquired."""
        result = await db_retry(
            self.db.execute,
            """
            INSERT INTO orchestrator_locks (todo_id, worker_id, expires_at)
            VALUES ($1, $2, NOW() + make_interval(secs => $3))
            ON CONFLICT (todo_id) DO NOTHING
            """,
            todo_id,
            self.worker_id,
            float(self.ttl_seconds),
        )
        return result == "INSERT 0 1"

    async def release(self, todo_id: str) -> None:
        """Release a lock we own."""
        await db_retry(
            self.db.execute,
            "DELETE FROM orchestrator_locks WHERE todo_id = $1 AND worker_id = $2",
            todo_id,
            self.worker_id,
        )

    async def heartbeat(self) -> None:
        """Extend TTL for all locks we own."""
        await db_retry(
            self.db.execute,
            """
            UPDATE orchestrator_locks
            SET heartbeat_at = NOW(),
                expires_at = NOW() + make_interval(secs => $2)
            WHERE worker_id = $1
            """,
            self.worker_id,
            float(self.ttl_seconds),
        )

    async def reclaim_expired(self) -> int:
        """Delete expired locks (from dead workers). Returns count reclaimed."""
        result = await db_retry(
            self.db.execute,
            "DELETE FROM orchestrator_locks WHERE expires_at < NOW()",
        )
        # result is like "DELETE 3"
        parts = result.split()
        return int(parts[1]) if len(parts) > 1 else 0
