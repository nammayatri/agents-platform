import asyncpg


class AuditLogger:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def log(
        self,
        action: str,
        *,
        todo_id: str | None = None,
        user_id: str | None = None,
        agent_run_id: str | None = None,
        detail: str = "",
        metadata: dict | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO audit_log (todo_id, user_id, agent_run_id, action, detail, metadata_json)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            todo_id,
            user_id,
            agent_run_id,
            action,
            detail,
            metadata or {},
        )
