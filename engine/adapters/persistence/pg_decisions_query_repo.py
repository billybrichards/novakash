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
        sd.regime,
        sd.fill_price,
        (sd.metadata_json->>'stake_usd')::numeric      AS stake_usd,
        CASE
            WHEN ws.close_price > ws.open_price THEN 'UP'
            WHEN ws.close_price < ws.open_price THEN 'DOWN'
            ELSE NULL
        END                                            AS actual_outcome,
        sd.evaluated_at
    FROM strategy_decisions sd
    LEFT JOIN window_snapshots ws
        ON sd.asset   = ws.asset
        AND sd.window_ts = ws.window_ts
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
