"""
PostgreSQL log repository — persists margin engine log records.

Provides both a low-level write method and a logging.Handler that buffers
records and batch-inserts them every flush_interval_s seconds.

Table: margin_logs (created idempotently on startup).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

CREATE_LOGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS margin_logs (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    extra JSONB
);
CREATE INDEX IF NOT EXISTS idx_margin_logs_ts ON margin_logs(ts);
CREATE INDEX IF NOT EXISTS idx_margin_logs_level ON margin_logs(level);
"""


class PgLogRepository:
    """Async log writer backed by asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_table(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_LOGS_TABLE_SQL)
        logger.info("margin_logs table ensured")

    async def write_batch(self, records: list[tuple]) -> int:
        """
        Insert a batch of log records.
        Each tuple: (ts, level, logger_name, message, extra_json)
        Returns number of rows inserted.
        """
        if not records:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO margin_logs (ts, level, logger, message, extra) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                records,
            )
        return len(records)

    async def query(
        self,
        limit: int = 100,
        level: Optional[str] = None,
        since_minutes: int = 60,
    ) -> list[dict]:
        """Query recent logs for the status API."""
        sql = (
            "SELECT ts, level, logger, message, extra FROM margin_logs "
            "WHERE ts > now() - make_interval(mins => $1)"
        )
        params: list = [since_minutes]
        if level:
            sql += f" AND level = ${len(params) + 1}"
            params.append(level.upper())
        sql += f" ORDER BY ts DESC LIMIT ${len(params) + 1}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            {
                "ts": row["ts"].isoformat(),
                "level": row["level"],
                "logger": row["logger"],
                "message": row["message"],
                "extra": row["extra"],
            }
            for row in rows
        ]


class AsyncPgLogHandler(logging.Handler):
    """
    Logging handler that buffers records and batch-inserts to PostgreSQL.

    Non-blocking: records are queued in memory, a background task flushes
    every flush_interval_s seconds. Drops oldest records if buffer exceeds
    max_buffer_size to avoid memory pressure.
    """

    def __init__(
        self,
        repo: PgLogRepository,
        loop: asyncio.AbstractEventLoop,
        flush_interval_s: float = 5.0,
        max_buffer_size: int = 2000,
        min_level: int = logging.INFO,
    ) -> None:
        super().__init__(level=min_level)
        self._repo = repo
        self._loop = loop
        self._buffer: list[tuple] = []
        self._flush_interval = flush_interval_s
        self._max_buffer = max_buffer_size
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = self._loop.create_task(self._flush_loop(), name="log-flusher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final flush
        await self._flush()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
            msg = self.format(record) if self.formatter else record.getMessage()
            extra = None
            if record.exc_info and record.exc_info[1]:
                extra = f'{{"exception": "{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"}}'

            self._buffer.append((ts, record.levelname, record.name, msg, extra))

            # Drop oldest if buffer overflow
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer:]
        except Exception:
            self.handleError(record)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        try:
            await self._repo.write_batch(batch)
        except Exception as e:
            # Don't use logger here to avoid recursion
            print(f"[log-flusher] DB write failed ({len(batch)} records): {e}")
