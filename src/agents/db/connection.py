import json
import logging
from pathlib import Path

import asyncpg

from agents.config.settings import settings

_pool: asyncpg.Pool | None = None
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up JSON/JSONB codecs so JSONB columns are returned as Python objects."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def _run_migrations(pool: asyncpg.Pool) -> None:
    """Auto-apply any SQL migrations that haven't been run yet."""
    async with pool.acquire() as conn:
        # Ensure tracking table exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Get already-applied migrations
        rows = await conn.fetch("SELECT filename FROM _migrations")
        applied = {r["filename"] for r in rows}

        # Discover and sort migration files
        if not MIGRATIONS_DIR.is_dir():
            return
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

        for sql_file in sql_files:
            if sql_file.name in applied:
                continue
            sql = sql_file.read_text()
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (filename) VALUES ($1)",
                    sql_file.name,
                )
                logger.info("Applied migration: %s", sql_file.name)
            except Exception:
                logger.exception("Migration failed: %s", sql_file.name)
                raise


async def init_db() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=5,
        max_size=20,
        init=_init_connection,
    )
    await _run_migrations(_pool)
    return _pool


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool
