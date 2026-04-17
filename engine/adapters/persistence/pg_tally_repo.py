"""PostgreSQL tally repo — implements ``TallyQueryPort``.

Aggregates WIN/LOSS/P&L from the ``trades`` table (live) plus hypothetical
shadow outcomes from ``shadow_decisions`` (ghost) for today/hour/session
rollups. 60-second TTL cache wraps each query to keep per-alert latency
under budget.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

import asyncpg
import structlog

from domain.alert_values import CumulativeTally
from domain.ports import TallyQueryPort

log = structlog.get_logger(__name__)

_CACHE_TTL_SEC = 60.0


class PgTallyRepo(TallyQueryPort):
    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_client: Optional[object] = None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client
        self._cache: dict[str, tuple[float, CumulativeTally | dict]] = {}

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            return getattr(self._db_client, "_pool", None)
        return None

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > _CACHE_TTL_SEC:
            return None
        return value

    def _cache_put(self, key: str, value) -> None:
        self._cache[key] = (time.time(), value)

    async def today(self) -> CumulativeTally:
        cached = self._cache_get("today")
        if cached is not None:
            return cached  # type: ignore[return-value]
        tally = await self._aggregate_trades(
            "AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
        )
        self._cache_put("today", tally)
        return tally

    async def last_hour(self) -> CumulativeTally:
        cached = self._cache_get("hour")
        if cached is not None:
            return cached  # type: ignore[return-value]
        tally = await self._aggregate_trades(
            "AND created_at >= NOW() - interval '1 hour'"
        )
        self._cache_put("hour", tally)
        return tally

    async def session(self, since_unix: int) -> CumulativeTally:
        cache_key = f"session:{since_unix}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        tally = await self._aggregate_trades(
            f"AND created_at >= to_timestamp({int(since_unix)})"
        )
        self._cache_put(cache_key, tally)
        return tally

    async def today_by_strategy(
        self,
    ) -> dict[tuple[str, str, str], CumulativeTally]:
        cached = self._cache_get("today_by_strategy")
        if cached is not None:
            return dict(cached)  # type: ignore[return-value]
        pool = self._get_pool()
        if not pool:
            return {}
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        timeframe,
                        strategy_id,
                        'LIVE' AS mode,
                        COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
                        COUNT(*) FILTER (WHERE outcome = 'LOSS') AS losses,
                        COALESCE(SUM(pnl_usd), 0)::text AS pnl
                    FROM trades
                    WHERE created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                      AND outcome IN ('WIN', 'LOSS')
                    GROUP BY timeframe, strategy_id
                    """
                )
        except Exception as exc:
            log.warning("pg_tally_repo.by_strategy_failed", error=str(exc)[:200])
            return {}
        out: dict[tuple[str, str, str], CumulativeTally] = {}
        for r in rows:
            tf = str(r["timeframe"])
            sid = str(r["strategy_id"])
            mode = str(r["mode"])
            out[(tf, sid, mode)] = CumulativeTally(
                wins=int(r["wins"]),
                losses=int(r["losses"]),
                pnl_usdc=Decimal(r["pnl"]),
                timeframe=tf,
                strategy_id=sid,
                mode=mode,
            )
        self._cache_put("today_by_strategy", out)
        return dict(out)

    async def today_combined(
        self, timeframe: Optional[str] = None
    ) -> CumulativeTally:
        if timeframe is None:
            t = await self.today()
            return t
        cache_key = f"today_combined:{timeframe}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        tally = await self._aggregate_trades(
            "AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
            "AND timeframe = $1",
            timeframe,
        )
        self._cache_put(cache_key, tally)
        return tally

    async def _aggregate_trades(
        self, where_clause: str, *params
    ) -> CumulativeTally:
        pool = self._get_pool()
        if not pool:
            return CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        query = f"""
            SELECT
                COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
                COUNT(*) FILTER (WHERE outcome = 'LOSS') AS losses,
                COALESCE(SUM(pnl_usd), 0)::text AS pnl
            FROM trades
            WHERE outcome IN ('WIN', 'LOSS') {where_clause}
        """
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(query, *params)
        except Exception as exc:
            log.warning("pg_tally_repo.aggregate_failed", error=str(exc)[:200])
            return CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        if row is None:
            return CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        return CumulativeTally(
            wins=int(row["wins"] or 0),
            losses=int(row["losses"] or 0),
            pnl_usdc=Decimal(row["pnl"]),
        )
