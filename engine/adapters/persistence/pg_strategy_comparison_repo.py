"""Adapter: PgStrategyComparisonRepo — Postgres implementation.

Writes computed `StrategyComparison` snapshots to the `strategy_comparison`
table. Idempotent via PK; same snapshot_at + cell key is a no-op.
"""

from __future__ import annotations

from typing import Optional, Sequence

import structlog

from domain.strategy_comparison.entities import StrategyComparison

log = structlog.get_logger(__name__)

_COLUMNS = [
    "snapshot_at",
    "strategy_id",
    "asset",
    "timeframe",
    "window_period",
    "t_band",
    "direction_filter",
    "regime_filter",
    "n_fires",
    "n_wins",
    "n_losses",
    "n_pending",
    "wr_pct",
    "wilson_low",
    "wilson_high",
    "avg_fill",
    "median_fill",
    "avg_stake_usd",
    "real_net_pnl_usd",
    "real_pnl_per_fire",
    "daily_run_rate_usd",
]


def _to_record(e: StrategyComparison) -> tuple:
    m = e.metrics
    return (
        e.snapshot_at,
        e.strategy_id,
        e.asset,
        e.timeframe,
        e.window_period.value,
        e.t_band.value,
        e.direction_filter.value,
        e.regime_filter.value,
        m.n_fires,
        m.n_wins,
        m.n_losses,
        m.n_pending,
        m.wr_pct,
        m.wilson_low,
        m.wilson_high,
        m.avg_fill,
        m.median_fill,
        m.avg_stake_usd,
        m.real_net_pnl_usd,
        m.real_pnl_per_fire,
        m.daily_run_rate_usd,
    )


class PgStrategyComparisonRepo:
    """Implements StrategyComparisonRepoPort using an asyncpg pool."""

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

    async def save(self, snapshot: Sequence[StrategyComparison]) -> int:
        """Bulk-insert via executemany; ON CONFLICT DO NOTHING at PK.

        copy_records_to_table has no conflict handling; the use case can
        emit two records that collide on the PK when the snapshot rolls
        over inside one transaction. ON CONFLICT preserves the first.
        """
        if not snapshot:
            return 0

        pool = self._get_pool()
        if not pool:
            log.warning("pg_strategy_comparison_repo.no_pool")
            return 0

        records = [_to_record(e) for e in snapshot]
        placeholders = ", ".join(f"${i}" for i in range(1, len(_COLUMNS) + 1))
        cols = ", ".join(_COLUMNS)
        sql = (
            f"INSERT INTO strategy_comparison ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT (snapshot_at, strategy_id, window_period, t_band, "
            "direction_filter, regime_filter) DO NOTHING"
        )
        try:
            async with pool.acquire() as conn:
                await conn.executemany(sql, records)
            log.info(
                "pg_strategy_comparison_repo.saved",
                n_rows=len(records),
            )
            return len(records)
        except Exception as exc:
            log.warning(
                "pg_strategy_comparison_repo.save_failed",
                error=str(exc)[:200],
                n_rows=len(records),
            )
            return 0

    async def prune_older_than_days(self, days: int) -> int:
        """DELETE FROM strategy_comparison WHERE snapshot_at < NOW() - INTERVAL."""
        pool = self._get_pool()
        if not pool:
            return 0
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM strategy_comparison "
                    "WHERE snapshot_at < NOW() - ($1 * INTERVAL '1 day')",
                    days,
                )
            # asyncpg returns "DELETE N" as a string
            deleted = int(result.split()[-1]) if result else 0
            log.info("pg_strategy_comparison_repo.pruned", deleted=deleted, days=days)
            return deleted
        except Exception as exc:
            log.warning(
                "pg_strategy_comparison_repo.prune_failed", error=str(exc)[:200]
            )
            return 0
