"""
Strategy decisions repository — persists V4 strategy decisions for backtesting.

Captures per-strategy evaluations at position entry time for post-trade analysis.
Table: margin_strategy_decisions (created idempotently on startup).

NOTE: Table is named margin_strategy_decisions (not strategy_decisions) because
the shared Railway DB already has a strategy_decisions table from the Polymarket
engine with a different schema.

Design:
- One row per strategy evaluation at position entry
- Full v4 snapshot captured in v4_snapshot JSONB
- Tracks decision, confidence, timescale, regime, size_mult, rationale
- Async batched writes similar to signal recorder
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

TABLE_NAME = "margin_strategy_decisions"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT 'BTC',
    strategy_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    confidence REAL,
    timescale TEXT,
    regime TEXT,
    v4_snapshot JSONB,
    rationale TEXT,
    size_mult REAL DEFAULT 1.0,
    hold_minutes INT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_msd_position_id ON {TABLE_NAME}(position_id);
CREATE INDEX IF NOT EXISTS idx_msd_asset_ts ON {TABLE_NAME}(asset, created_at);
CREATE INDEX IF NOT EXISTS idx_msd_strategy_id ON {TABLE_NAME}(strategy_id);
"""

INSERT_DECISION_SQL = f"""
INSERT INTO {TABLE_NAME}
    (id, position_id, asset, strategy_id, decision, confidence, timescale, regime,
     v4_snapshot, rationale, size_mult, hold_minutes)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
ON CONFLICT (id) DO NOTHING
"""


class PgStrategyDecisionRepository:
    """PostgreSQL strategy decision repository with batched inserts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_table(self) -> None:
        """Create table if it doesn't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        logger.info("%s table ensured", TABLE_NAME)

    async def save(self, decision: dict) -> None:
        """Save a single strategy decision."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                INSERT_DECISION_SQL,
                decision["id"],
                decision["position_id"],
                decision["asset"],
                decision["strategy_id"],
                decision["decision"],
                decision.get("confidence"),
                decision.get("timescale"),
                decision.get("regime"),
                json.dumps(decision.get("v4_snapshot", {})),
                decision.get("rationale"),
                decision.get("size_mult", 1.0),
                decision.get("hold_minutes"),
            )

    async def save_batch(self, decisions: list[dict]) -> None:
        """Save multiple strategy decisions in a batch."""
        if not decisions:
            return

        rows = [
            (
                d["id"],
                d["position_id"],
                d["asset"],
                d["strategy_id"],
                d["decision"],
                d.get("confidence"),
                d.get("timescale"),
                d.get("regime"),
                json.dumps(d.get("v4_snapshot", {})),
                d.get("rationale"),
                d.get("size_mult", 1.0),
                d.get("hold_minutes"),
            )
            for d in decisions
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(INSERT_DECISION_SQL, rows)
        logger.debug("Saved %d strategy decisions", len(decisions))

    async def get_by_position(self, position_id: str) -> list[dict]:
        """Get all strategy decisions for a position."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {TABLE_NAME} WHERE position_id = $1 ORDER BY strategy_id",
                position_id,
            )
        return [dict(r) for r in rows]

    async def get_decisions_with_outcomes(
        self,
        limit: int = 100,
        offset: int = 0,
        strategy_id: Optional[str] = None,
        asset: str = "BTC",
    ) -> list[dict]:
        """
        Get strategy decisions joined to position outcomes for backtesting.

        Returns decisions with actual entry/exit prices, PnL, and exit reason.
        """
        query = f"""
            SELECT
                sd.id,
                sd.position_id,
                sd.asset,
                sd.strategy_id,
                sd.decision,
                sd.confidence,
                sd.timescale,
                sd.regime,
                sd.v4_snapshot,
                sd.rationale,
                sd.size_mult,
                sd.hold_minutes,
                sd.created_at,
                mp.entry_price,
                mp.exit_price,
                mp.realised_pnl,
                mp.exit_reason,
                mp.opened_at,
                mp.closed_at,
                mp.v4_entry_regime,
                mp.v4_entry_macro_bias,
                mp.v4_entry_expected_move_bps,
                mp.v4_entry_composite_v3
            FROM {TABLE_NAME} sd
            LEFT JOIN margin_positions mp ON mp.id = sd.position_id
            WHERE sd.asset = $1
              AND ($2::text IS NULL OR sd.strategy_id = $2)
            ORDER BY sd.created_at DESC
            LIMIT $3 OFFSET $4
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, asset, strategy_id, limit, offset)
        return [dict(r) for r in rows]

    async def get_stats_by_strategy(
        self,
        asset: str = "BTC",
    ) -> dict[str, dict]:
        """
        Get aggregated statistics for each strategy.

        Returns win rate, total trades, PnL by strategy.
        """
        query = f"""
            SELECT
                sd.strategy_id,
                COUNT(*) AS n_decisions,
                COUNT(*) FILTER (WHERE sd.decision IN ('TRADE_LONG', 'TRADE_SHORT')) AS n_trades,
                COUNT(*) FILTER (
                    WHERE sd.decision IN ('TRADE_LONG', 'TRADE_SHORT')
                    AND mp.realised_pnl > 0
                ) AS n_wins,
                AVG(sd.confidence) AS avg_confidence,
                SUM(mp.realised_pnl) AS total_pnl,
                AVG(mp.realised_pnl) AS avg_pnl,
                MIN(mp.realised_pnl) AS min_pnl,
                MAX(mp.realised_pnl) AS max_pnl
            FROM {TABLE_NAME} sd
            LEFT JOIN margin_positions mp ON mp.id = sd.position_id
            WHERE sd.asset = $1
            GROUP BY sd.strategy_id
            ORDER BY sd.strategy_id
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, asset)

        result = {}
        for r in rows:
            strategy_id = r["strategy_id"]
            n_trades = r["n_trades"] or 0
            n_wins = r["n_wins"] or 0

            result[strategy_id] = {
                "n_decisions": r["n_decisions"] or 0,
                "n_trades": n_trades,
                "n_wins": n_wins,
                "win_rate": round(n_wins / n_trades, 4) if n_trades > 0 else None,
                "avg_confidence": round(r["avg_confidence"], 4)
                if r["avg_confidence"]
                else None,
                "total_pnl": float(r["total_pnl"]) if r["total_pnl"] else 0.0,
                "avg_pnl": float(r["avg_pnl"]) if r["avg_pnl"] else 0.0,
                "min_pnl": float(r["min_pnl"]) if r["min_pnl"] else 0.0,
                "max_pnl": float(r["max_pnl"]) if r["max_pnl"] else 0.0,
            }

        return result


class AsyncStrategyDecisionRecorder:
    """
    Buffers strategy decisions and flushes to DB periodically.

    Non-blocking: strategy services call record_decision() which only appends
    to a buffer. A background task flushes every flush_interval_s.
    """

    def __init__(
        self,
        repo: PgStrategyDecisionRepository,
        loop: asyncio.AbstractEventLoop,
        flush_interval_s: float = 5.0,
        max_buffer_size: int = 500,
    ) -> None:
        self._repo = repo
        self._loop = loop
        self._buffer: list[dict] = []
        self._flush_interval = flush_interval_s
        self._max_buffer = max_buffer_size
        self._task: Optional[asyncio.Task] = None
        self._recorded = 0
        self._flushed = 0
        self._dropped = 0

    def start(self) -> None:
        """Start the background flush task."""
        self._task = self._loop.create_task(
            self._flush_loop(), name="strategy-decision-recorder"
        )
        logger.info(
            "Strategy decision recorder started (flush_interval=%ds)",
            self._flush_interval,
        )

    async def stop(self) -> None:
        """Stop the recorder and flush remaining decisions."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()
        logger.info(
            "Strategy decision recorder stopped — recorded=%d flushed=%d dropped=%d",
            self._recorded,
            self._flushed,
            self._dropped,
        )

    def record_decision(
        self,
        position_id: str,
        strategy_id: str,
        decision: str,
        asset: str = "BTC",
        confidence: Optional[float] = None,
        timescale: Optional[str] = None,
        regime: Optional[str] = None,
        v4_snapshot: Optional[dict] = None,
        rationale: Optional[str] = None,
        size_mult: float = 1.0,
        hold_minutes: Optional[int] = None,
    ) -> None:
        """
        Record a strategy decision. Called by strategy services at decision time.

        Args:
            position_id: ID of the position this decision relates to
            strategy_id: Strategy identifier (e.g., "fee_aware_15m")
            decision: Decision type ("TRADE_LONG", "TRADE_SHORT", "NO_TRADE")
            asset: Asset being traded
            confidence: Decision confidence (0-1)
            timescale: Timescale (e.g., "5m", "15m")
            regime: Market regime at decision time
            v4_snapshot: Full v4 snapshot at decision time
            rationale: Reason string from the strategy
            size_mult: Position sizing multiplier
            hold_minutes: Expected holding period
        """
        import uuid

        try:
            row = {
                "id": str(uuid.uuid4())[:12],
                "position_id": position_id,
                "asset": asset,
                "strategy_id": strategy_id,
                "decision": decision,
                "confidence": confidence,
                "timescale": timescale,
                "regime": regime,
                "v4_snapshot": v4_snapshot or {},
                "rationale": rationale,
                "size_mult": size_mult,
                "hold_minutes": hold_minutes,
            }
            self._buffer.append(row)
            self._recorded += 1

            if len(self._buffer) > self._max_buffer:
                drop = len(self._buffer) - self._max_buffer
                self._buffer = self._buffer[drop:]
                self._dropped += drop
                logger.warning(
                    "Strategy decision buffer overflow: dropped %d records", drop
                )
        except Exception as e:
            # Never let recording break the strategy evaluation
            logger.warning("Strategy decision record failed: %s", e)

    async def _flush_loop(self) -> None:
        """Background loop that flushes the buffer periodically."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()

    async def _flush(self) -> None:
        """Flush the buffer to the database."""
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer.clear()

        try:
            await self._repo.save_batch(batch)
            self._flushed += len(batch)
        except Exception as e:
            logger.error(
                "Strategy decision batch save failed (%d rows): %s", len(batch), e
            )
            # Restore buffer on error so we don't lose data
            self._buffer = batch
