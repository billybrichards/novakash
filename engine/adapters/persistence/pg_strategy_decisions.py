"""PostgreSQL Strategy Decision Repository -- persistence for strategy decisions.

Implements :class:`engine.domain.ports.StrategyDecisionRepository` by writing
to the ``strategy_decisions`` table.

Audit: SP-05.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import structlog

from domain.ports import StrategyDecisionRepository
from domain.value_objects import StrategyDecisionRecord

log = structlog.get_logger(__name__)


class PgStrategyDecisionRepository(StrategyDecisionRepository):
    """asyncpg-backed strategy decision repository.

    Accepts an ``asyncpg.Pool`` -- the same pool the legacy ``DBClient`` uses.
    """

    def __init__(
        self, pool: Optional[asyncpg.Pool] = None, db_client: Optional[object] = None
    ) -> None:
        self._pool = pool
        self._db_client = db_client  # fallback: extract pool lazily from DBClient

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            # DBClient stores pool as _pool attribute
            return getattr(self._db_client, "_pool", None)
        return None

    async def write_decision(self, decision: StrategyDecisionRecord) -> None:
        """Persist one strategy decision row.

        Idempotent by (strategy_id, asset, window_ts, eval_offset) via
        ON CONFLICT DO UPDATE.
        """
        pool = self._get_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategy_decisions (
                        strategy_id, strategy_version, asset, window_ts,
                        timeframe, eval_offset, mode,
                        action, direction, confidence, confidence_score,
                        entry_cap, collateral_pct, entry_reason, skip_reason,
                        executed, order_id, fill_price, fill_size,
                        metadata_json, evaluated_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7,
                        $8, $9, $10, $11,
                        $12, $13, $14, $15,
                        $16, $17, $18, $19,
                        $20::jsonb, $21
                    )
                    ON CONFLICT (strategy_id, asset, window_ts, eval_offset)
                    DO UPDATE SET
                        action = EXCLUDED.action,
                        direction = EXCLUDED.direction,
                        confidence = EXCLUDED.confidence,
                        confidence_score = EXCLUDED.confidence_score,
                        entry_cap = EXCLUDED.entry_cap,
                        collateral_pct = EXCLUDED.collateral_pct,
                        entry_reason = EXCLUDED.entry_reason,
                        skip_reason = EXCLUDED.skip_reason,
                        metadata_json = EXCLUDED.metadata_json,
                        evaluated_at = EXCLUDED.evaluated_at
                    """,
                    decision.strategy_id,
                    decision.strategy_version,
                    decision.asset,
                    decision.window_ts,
                    decision.timeframe,
                    decision.eval_offset,
                    decision.mode,
                    decision.action,
                    decision.direction,
                    decision.confidence,
                    decision.confidence_score,
                    decision.entry_cap,
                    decision.collateral_pct,
                    decision.entry_reason,
                    decision.skip_reason,
                    decision.executed,
                    decision.order_id,
                    decision.fill_price,
                    decision.fill_size,
                    decision.metadata_json,
                    datetime.fromtimestamp(decision.evaluated_at, tz=timezone.utc)
                    if decision.evaluated_at
                    else datetime.now(timezone.utc),
                )
        except Exception as exc:
            log.warning("pg_strategy_decisions.write_error", error=str(exc)[:200])

    async def get_decisions_for_window(
        self,
        asset: str,
        window_ts: int,
    ) -> list[StrategyDecisionRecord]:
        """Read all strategy decisions for a window."""
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT strategy_id, strategy_version, asset, window_ts,
                           timeframe, eval_offset, mode,
                           action, direction, confidence, confidence_score,
                           entry_cap, collateral_pct, entry_reason, skip_reason,
                           executed, order_id, fill_price, fill_size,
                           metadata_json::text, evaluated_at
                    FROM strategy_decisions
                    WHERE asset = $1 AND window_ts = $2
                    ORDER BY eval_offset, strategy_id
                    """,
                    asset,
                    window_ts,
                )
            return [
                StrategyDecisionRecord(
                    strategy_id=r["strategy_id"],
                    strategy_version=r["strategy_version"],
                    asset=r["asset"],
                    window_ts=r["window_ts"],
                    timeframe=r["timeframe"],
                    eval_offset=r["eval_offset"],
                    mode=r["mode"],
                    action=r["action"],
                    direction=r["direction"],
                    confidence=r["confidence"],
                    confidence_score=r["confidence_score"],
                    entry_cap=r["entry_cap"],
                    collateral_pct=r["collateral_pct"],
                    entry_reason=r["entry_reason"],
                    skip_reason=r["skip_reason"],
                    executed=r["executed"],
                    order_id=r["order_id"],
                    fill_price=r["fill_price"],
                    fill_size=r["fill_size"],
                    metadata_json=r["metadata_json"] or "{}",
                    evaluated_at=r["evaluated_at"].timestamp()
                    if r["evaluated_at"]
                    else 0.0,
                )
                for r in rows
            ]
        except Exception as exc:
            log.warning("pg_strategy_decisions.read_error", error=str(exc)[:200])
            return []

    async def get_decisions_in_range(
        self,
        *,
        asset: str,
        timeframe: str,
        strategy_id: str,
        start_window_ts: int,
        end_window_ts: int,
    ) -> list[StrategyDecisionRecord]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT strategy_id, strategy_version, asset, window_ts,
                           timeframe, eval_offset, mode,
                           action, direction, confidence, confidence_score,
                           entry_cap, collateral_pct, entry_reason, skip_reason,
                           executed, order_id, fill_price, fill_size,
                           metadata_json::text, evaluated_at
                    FROM strategy_decisions
                    WHERE asset = $1
                      AND timeframe = $2
                      AND strategy_id = $3
                      AND window_ts BETWEEN $4 AND $5
                    ORDER BY window_ts DESC, eval_offset DESC
                    """,
                    asset,
                    timeframe,
                    strategy_id,
                    start_window_ts,
                    end_window_ts,
                )
            return [
                StrategyDecisionRecord(
                    strategy_id=r["strategy_id"],
                    strategy_version=r["strategy_version"],
                    asset=r["asset"],
                    window_ts=r["window_ts"],
                    timeframe=r["timeframe"],
                    eval_offset=r["eval_offset"],
                    mode=r["mode"],
                    action=r["action"],
                    direction=r["direction"],
                    confidence=r["confidence"],
                    confidence_score=r["confidence_score"],
                    entry_cap=r["entry_cap"],
                    collateral_pct=r["collateral_pct"],
                    entry_reason=r["entry_reason"],
                    skip_reason=r["skip_reason"],
                    executed=r["executed"],
                    order_id=r["order_id"],
                    fill_price=r["fill_price"],
                    fill_size=r["fill_size"],
                    metadata_json=r["metadata_json"] or "{}",
                    evaluated_at=r["evaluated_at"].timestamp()
                    if r["evaluated_at"]
                    else 0.0,
                )
                for r in rows
            ]
        except Exception as exc:
            log.warning("pg_strategy_decisions.read_range_error", error=str(exc)[:200])
            return []
