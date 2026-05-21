"""PostgreSQL exposure repository — backs ``ExposureRepository`` (Hub #554).

Three indexed SUM queries over the ``trades`` table that feed
``use_cases.exposure_caps.check_exposure_caps``:

  - get_window_fresh_stake_usd
  - get_asset_open_exposure_usd
  - get_daily_fresh_stake_usd

All queries:
  * EXCLUDE ``is_secondary_fill = TRUE`` rows (Hub #554 Layer 1 added
    that column so a single on-chain double-fill writes two trade
    rows — counting both would double the apparent exposure).
  * Only count fills (``fill_size > 0``) so cancelled / unfilled FAK
    attempts do NOT pre-eat the cap.
  * Return ``0.0`` on any DB error or pool absence — the cap check
    treats 0.0 as "no existing exposure" and lets the trade through.
    Fail-OPEN is correct here: the engine has higher-level kill
    switches (drawdown, daily PnL limit) and we'd rather fire a trade
    we should have blocked than freeze the strategy on a transient
    DB hiccup. Operators can tighten this later with a fail-closed
    env flag if needed.
"""

from __future__ import annotations

from typing import Optional

import asyncpg
import structlog

log = structlog.get_logger(__name__)


class PgExposureCapRepository:
    """asyncpg-backed exposure aggregator.

    Accepts the engine-wide ``asyncpg.Pool``. Methods are async,
    idempotent, side-effect-free, and complete in a single round-trip.
    """

    def __init__(self, pool: Optional[asyncpg.Pool]) -> None:
        self._pool = pool

    async def get_window_fresh_stake_usd(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
    ) -> float:
        """Sum stake on already-filled rows for (asset, window_ts, timeframe).

        Matches per-(asset, window_ts, timeframe) regardless of
        ``strategy_id`` — that's the whole point: the cap exists to
        block the cross-strategy correlated-double-down problem when
        v9 / v9.2 / v12_combo all fire DOWN on the same window.
        """
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(stake_usd), 0)::float
                      FROM trades
                     WHERE COALESCE(metadata->>'asset', 'BTC') = $1
                       AND (metadata->>'window_ts')::bigint = $2
                       AND COALESCE(metadata->>'timeframe', '5m') = $3
                       AND COALESCE(fill_size, 0) > 0
                       AND COALESCE(is_secondary_fill, FALSE) = FALSE
                    """,
                    asset,
                    int(window_ts),
                    timeframe,
                )
                return float(row or 0.0)
        except Exception as exc:
            log.warning(
                "pg_exposure.window_stake_failed",
                asset=asset,
                window_ts=window_ts,
                error=str(exc)[:120],
            )
            return 0.0

    async def get_asset_open_exposure_usd(self, asset: str) -> float:
        """Sum stake on OPEN positions for ``asset``.

        "Open" = no resolved_at, status not in resolved/expired terminal
        states. This is the in-flight exposure cap — protects the wallet
        when multiple windows have unresolved fills outstanding (e.g.
        slow Polymarket resolution).
        """
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(stake_usd), 0)::float
                      FROM trades
                     WHERE COALESCE(metadata->>'asset', 'BTC') = $1
                       AND COALESCE(fill_size, 0) > 0
                       AND COALESCE(is_secondary_fill, FALSE) = FALSE
                       AND resolved_at IS NULL
                       AND COALESCE(UPPER(status), '') NOT IN (
                           'RESOLVED_WIN', 'RESOLVED_LOSS', 'EXPIRED', 'CANCELLED', 'CANCELED'
                       )
                    """,
                    asset,
                )
                return float(row or 0.0)
        except Exception as exc:
            log.warning(
                "pg_exposure.asset_open_failed",
                asset=asset,
                error=str(exc)[:120],
            )
            return 0.0

    async def get_daily_fresh_stake_usd(self, asset: str) -> float:
        """Sum stake placed since UTC midnight today, for ``asset``.

        Cap is on "fresh stake" (entries placed today), not realised
        loss. Pairs well with the existing daily_loss_limit_pct
        guard — together they bound both worst-case drawdown and
        signal-density misfires.
        """
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(stake_usd), 0)::float
                      FROM trades
                     WHERE COALESCE(metadata->>'asset', 'BTC') = $1
                       AND COALESCE(fill_size, 0) > 0
                       AND COALESCE(is_secondary_fill, FALSE) = FALSE
                       AND created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
                    """,
                    asset,
                )
                return float(row or 0.0)
        except Exception as exc:
            log.warning(
                "pg_exposure.daily_stake_failed",
                asset=asset,
                error=str(exc)[:120],
            )
            return 0.0
