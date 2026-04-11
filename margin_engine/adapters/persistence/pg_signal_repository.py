"""
PostgreSQL signal repository — passively records every composite score
received from TimesFM v3 WS, regardless of whether the engine trades on it.

Purpose: build a labelled time-series dataset for offline analysis of signal
edge (forward-return prediction, autocorrelation, threshold sweeps). The
table is write-only from the engine's perspective — no trading logic reads it.

Table: margin_signals (created idempotently on startup).

Design:
- Every `composite_score` WS message → one row
- Full JSONB payload stored in `signals_json` + `cascade_json` for flexibility
- Top-level columns for the most-queried fields (timescale, composite, ts)
- Batched writes via AsyncPgSignalRecorder (same pattern as the log handler)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

CREATE_SIGNALS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS margin_signals (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    asset TEXT NOT NULL DEFAULT 'BTC',
    timescale TEXT NOT NULL,
    composite REAL,
    -- break-out commonly-queried individual signal components
    elm REAL,
    cascade REAL,
    taker REAL,
    oi REAL,
    funding REAL,
    vpin REAL,
    momentum REAL,
    -- cascade FSM state
    cascade_strength REAL,
    cascade_tau1 REAL,
    cascade_tau2 REAL,
    cascade_exhaustion_t REAL,
    -- full payload for whatever we forgot to extract
    signals_json JSONB,
    cascade_json JSONB,
    received_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_margin_signals_ts ON margin_signals(ts);
CREATE INDEX IF NOT EXISTS idx_margin_signals_timescale_ts ON margin_signals(timescale, ts);
CREATE INDEX IF NOT EXISTS idx_margin_signals_composite ON margin_signals(composite) WHERE composite IS NOT NULL;
"""

INSERT_SIGNAL_SQL = """
INSERT INTO margin_signals
  (ts, asset, timescale, composite,
   elm, cascade, taker, oi, funding, vpin, momentum,
   cascade_strength, cascade_tau1, cascade_tau2, cascade_exhaustion_t,
   signals_json, cascade_json)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb, $17::jsonb)
"""


def _safe_float(v: Any) -> Optional[float]:
    """Coerce to float, returning None for NaN/Inf/invalid."""
    if v is None:
        return None
    try:
        f = float(v)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


class PgSignalRepository:
    """Write-mostly signal repository with batched inserts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_table(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_SIGNALS_TABLE_SQL)
        logger.info("margin_signals table ensured")

    async def write_batch(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(INSERT_SIGNAL_SQL, rows)
        return len(rows)


class AsyncPgSignalRecorder:
    """
    Buffers incoming composite_score messages and flushes to DB periodically.

    Non-blocking: the WS adapter calls record(msg) which only appends to a list.
    A background task flushes every flush_interval_s. If the buffer overflows
    (DB unreachable for a long time), oldest records are dropped.
    """

    def __init__(
        self,
        repo: PgSignalRepository,
        loop: asyncio.AbstractEventLoop,
        flush_interval_s: float = 5.0,
        max_buffer_size: int = 5000,
    ) -> None:
        self._repo = repo
        self._loop = loop
        self._buffer: list[tuple] = []
        self._flush_interval = flush_interval_s
        self._max_buffer = max_buffer_size
        self._task: Optional[asyncio.Task] = None
        self._recorded = 0
        self._flushed = 0
        self._dropped = 0

    def start(self) -> None:
        self._task = self._loop.create_task(self._flush_loop(), name="signal-recorder")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()
        logger.info(
            "Signal recorder stopped — recorded=%d flushed=%d dropped=%d",
            self._recorded, self._flushed, self._dropped,
        )

    def record(self, msg: dict) -> None:
        """Called by WsSignalAdapter for every composite_score message."""
        try:
            ts_epoch = _safe_float(msg.get("ts"))
            ts = datetime.fromtimestamp(ts_epoch or 0, tz=timezone.utc) if ts_epoch else datetime.now(timezone.utc)
            asset = msg.get("asset", "BTC")
            timescale = msg.get("timescale", "unknown")
            signals = msg.get("signals") or {}
            cascade = msg.get("cascade") or {}

            row = (
                ts,
                asset,
                timescale,
                _safe_float(msg.get("composite")),
                _safe_float(signals.get("elm")),
                _safe_float(signals.get("cascade")),
                _safe_float(signals.get("taker")),
                _safe_float(signals.get("oi")),
                _safe_float(signals.get("funding")),
                _safe_float(signals.get("vpin")),
                _safe_float(signals.get("momentum")),
                _safe_float(cascade.get("strength")),
                _safe_float(cascade.get("tau1")),
                _safe_float(cascade.get("tau2")),
                _safe_float(cascade.get("exhaustion_t")),
                json.dumps(signals),
                json.dumps(cascade),
            )
            self._buffer.append(row)
            self._recorded += 1

            if len(self._buffer) > self._max_buffer:
                drop = len(self._buffer) - self._max_buffer
                self._buffer = self._buffer[drop:]
                self._dropped += drop
        except Exception as e:
            # Never let recording break the WS loop
            logger.warning("Signal record failed: %s", e)

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
            n = await self._repo.write_batch(batch)
            self._flushed += n
        except Exception as e:
            print(f"[signal-recorder] DB write failed ({len(batch)} rows): {e}")
