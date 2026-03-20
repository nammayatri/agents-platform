"""Async retry wrapper for transient database errors.

Retries on connection-level failures (dead connection, pool timeout,
interface errors) with exponential backoff.  Does NOT retry on query-level
errors (syntax, constraint violations, etc.) since those are deterministic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

import asyncpg

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_EXCEPTIONS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    OSError,  # covers connection reset, broken pipe
)

BACKOFF_DELAYS = (0.1, 0.5, 1.0)


async def db_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    """Call an async function with retry on transient DB errors.

    Usage::

        result = await db_retry(db.fetch, "SELECT ...", param1)
        result = await db_retry(db.execute, "UPDATE ...", p1, p2)
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except TRANSIENT_EXCEPTIONS as e:
            last_exc = e
            if attempt < max_retries:
                delay = BACKOFF_DELAYS[min(attempt, len(BACKOFF_DELAYS) - 1)]
                logger.warning(
                    "Transient DB error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Transient DB error persisted after %d retries: %s",
                    max_retries, e,
                )
    raise last_exc  # type: ignore[misc]
