"""Adapter: PgDecisionsQueryRepo — read-only Postgres adapter.

Joins `strategy_decisions` with `window_snapshots` to get outcome and
returns DecisionFireRow records ready for the comparison use case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

import structlog

from use_cases.ports.decisions_query_repo import DecisionFireRow

log = structlog.get_logger(__name__)

_SQL = """
    SELECT
        sd.strategy_id,
        sd.asset,
        sd.timeframe,
        sd.window_ts,
        sd.eval_offset,
        sd.direction,
        ws.regime,
        COALESCE(sd.fill_price, t.fill_price)          AS fill_price,
        t.stake_usd                                    AS stake_usd,
        COALESCE(se.outcome, ws.outcome)               AS actual_outcome,
        sd.evaluated_at
    FROM strategy_decisions sd
    LEFT JOIN trades t ON t.order_id = sd.order_id
    LEFT JOIN LATERAL (
        SELECT outcome
        FROM signal_evaluations
        WHERE asset = sd.asset
          AND window_ts = sd.window_ts
          AND timeframe = sd.timeframe
          AND outcome IS NOT NULL
        ORDER BY eval_offset DESC NULLS LAST
        LIMIT 1
    ) se ON TRUE
    LEFT JOIN LATERAL (
        SELECT outcome, regime
        FROM window_snapshots
        WHERE asset = sd.asset AND window_ts = sd.window_ts
        ORDER BY (outcome IS NOT NULL) DESC, eval_offset DESC NULLS LAST
        LIMIT 1
    ) ws ON TRUE
    WHERE sd.action      = 'TRADE'
      AND sd.executed    = TRUE
      AND sd.evaluated_at >= $1
      AND sd.evaluated_at <  $2
    ORDER BY sd.evaluated_at
"""


class PgDecisionsQueryRepo:
    """Implements DecisionsQueryRepoPort."""

    def __init__(
        self,
        pool=None,
        db_client=None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client

    def _get_pool(self):
        if self._pool:
            return self._pool
        if self._db_client:
            return getattr(self._db_client, "_pool", None)
        return None

    async def fires_with_outcomes(
        self, *, since: datetime, until: datetime
    ) -> Sequence[DecisionFireRow]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(_SQL, since, until)
        except Exception as exc:
            log.warning(
                "pg_decisions_query_repo.fetch_failed", error=str(exc)[:200]
            )
            return []

        result: list[DecisionFireRow] = []
        for r in rows:
            fill = r["fill_price"]
            stake = r["stake_usd"]
            result.append(
                DecisionFireRow(
                    strategy_id=str(r["strategy_id"]),
                    asset=str(r["asset"]),
                    timeframe=str(r["timeframe"]),
                    window_ts=int(r["window_ts"]),
                    eval_offset=int(r["eval_offset"]) if r["eval_offset"] is not None else None,
                    direction=str(r["direction"]) if r["direction"] else "UP",
                    regime=str(r["regime"]) if r["regime"] else None,
                    fill_price=float(fill) if fill is not None else None,
                    stake_usd=float(stake) if stake is not None else None,
                    actual_outcome=r["actual_outcome"],
                    evaluated_at=r["evaluated_at"],
                )
            )
        return result
