"""PgWindowExecutionGuard — DB-backed strategy dedup with in-memory cache.

FAIL-CLOSED: DB errors return True (block trade, don't double-fill).
"""
from __future__ import annotations

import structlog

from domain.ports import WindowExecutionGuard

log = structlog.get_logger()


class PgWindowExecutionGuard(WindowExecutionGuard):
    """DB-backed strategy dedup with in-memory read-through cache.

    FAIL-CLOSED: DB errors return True (block trade, don't double-fill).
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self._cache: set[tuple[str, int]] = set()

    async def has_executed(self, strategy_id: str, window_ts: int) -> bool:
        key = (strategy_id, window_ts)
        if key in self._cache:
            return True
        try:
            row = await self._pool.fetchrow(
                "SELECT 1 FROM strategy_executions "
                "WHERE strategy_id=$1 AND window_ts=$2",
                strategy_id, window_ts,
            )
            if row:
                self._cache.add(key)
                return True
            return False
        except Exception as exc:
            log.error("execution_guard.db_error", error=str(exc)[:120])
            return True  # FAIL-CLOSED

    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None:
        key = (strategy_id, window_ts)
        try:
            await self._pool.execute(
                "INSERT INTO strategy_executions (strategy_id, window_ts, order_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                strategy_id, window_ts, order_id,
            )
            self._cache.add(key)
        except Exception as exc:
            log.error("execution_guard.mark_error", error=str(exc)[:120])
            # Still add to in-memory cache to prevent same-process duplicates
            self._cache.add(key)

    async def load_recent(self, hours: int = 2) -> None:
        try:
            rows = await self._pool.fetch(
                "SELECT strategy_id, window_ts FROM strategy_executions "
                "WHERE executed_at > NOW() - ($1 || ' hours')::interval",
                str(hours),
            )
            for row in rows:
                self._cache.add((row["strategy_id"], row["window_ts"]))
            log.info("execution_guard.cache_warmed", entries=len(self._cache))
        except Exception as exc:
            log.warning("execution_guard.load_error", error=str(exc)[:120])
