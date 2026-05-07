"""PostgreSQL Cell Pause Repository.

Concrete implementation of the ``CellPauseRepo`` Protocol defined in
``services.rolling_wr_monitor``. Backed by the ``cell_pauses`` table
(migration: ``engine/db/migrations/add_cell_pauses.sql``).

Audits #379 + #385 (2026-05-06).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from services.cell_bucketing import session_label as _session
from services.cell_bucketing import t_band as _t_band
from services.rolling_wr_monitor import CellKey, ResolvedTrade

log = logging.getLogger(__name__)


class PgCellPauseRepo:
    """asyncpg-backed implementation of the CellPauseRepo Protocol.

    Never raises out of its public methods — logs errors and returns
    safe defaults. The hot-path gate reads ``is_cell_paused`` on every
    strategy evaluation; it must not crash the evaluation loop.

    Args:
        pool: asyncpg.Pool (direct). Mutually exclusive with db_client.
        db_client: Legacy DBClient shim — the repo extracts ``._pool``
            lazily, which means it survives DB reconnects.
    """

    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_client: Optional[object] = None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            # Support both DBClientLegacyShim and raw DBClient shapes.
            inner = getattr(self._db_client, "_inner", self._db_client)
            return getattr(inner, "_pool", None)
        return None

    async def ensure_tables(self) -> None:
        """Create ``cell_pauses`` and its indexes if they don't exist.

        Called once at engine startup. Idempotent — safe to re-run.
        Mirrors the pattern used by ``PgRedeemAttemptsRepository`` so the
        migration SQL file is the canonical spec but the engine never
        crashes on a fresh DB that hasn't had the migration applied.

        B2 FIX: matches the migration SQL exactly — creates the partial
        unique index ``idx_cell_pauses_one_active`` (WHERE released_at IS NULL)
        instead of an inline UNIQUE constraint that would allow concurrent
        inserts at different microsecond paused_at values.
        """
        pool = self._get_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cell_pauses (
                        id                 BIGSERIAL    PRIMARY KEY,
                        strategy_id        TEXT         NOT NULL,
                        direction          TEXT         NOT NULL,
                        t_band             TEXT         NOT NULL,
                        regime             TEXT,
                        session            TEXT,
                        paused_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                        pause_until        TIMESTAMPTZ  NOT NULL,
                        reason             TEXT         NOT NULL,
                        trigger_metric     JSONB,
                        released_at        TIMESTAMPTZ,
                        released_by        TEXT,
                        UNIQUE (strategy_id, direction, t_band, regime,
                                session, paused_at)
                    )
                    """
                )
                # B2 FIX: partial unique index so that concurrent inserts
                # (while released_at IS NULL) raise a UNIQUE violation rather
                # than both succeeding (C4 race condition fixed in PR #494).
                # CREATE INDEX IF NOT EXISTS is idempotent on fresh + migrated DBs.
                await conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_cell_pauses_one_active
                        ON cell_pauses (
                            strategy_id, direction, t_band,
                            COALESCE(regime, ''), COALESCE(session, '')
                        )
                        WHERE released_at IS NULL
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cell_pauses_active
                        ON cell_pauses (strategy_id, direction, t_band,
                                        regime)
                        WHERE released_at IS NULL
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cell_pauses_paused_at
                        ON cell_pauses (paused_at DESC)
                    """
                )
            log.info("pg_cell_pause_repo.tables_ensured")
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.ensure_tables_failed: %s",
                str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # CellPauseRepo Protocol implementation
    # ------------------------------------------------------------------

    async def get_recent_trades_for_cell(
        self, cell: CellKey, lookback_seconds: int
    ) -> list[ResolvedTrade]:
        """Return trades resolved in the last ``lookback_seconds`` for cell.

        Queries the ``trades`` table using the standard cell bucketing
        logic (t_band from eval_offset, session from hour_utc). Returns
        an empty list on any error.

        B1 FIX (SQL side): bucket by EXTRACT(HOUR FROM created_at) so the
        session label matches the session the trade FIRED in, not when
        Polymarket happened to settle it. A trade fired at 17:55 UTC that
        settles at 18:01 UTC belongs to us_pm (14-17), not us_late (18-21).
        """
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        strategy          AS strategy_id,
                        direction,
                        (metadata->>'eval_offset')::int   AS eval_offset,
                        EXTRACT(HOUR FROM created_at)::int AS hour_utc,
                        (metadata->>'vpin_regime')        AS regime,
                        fill_price,
                        pnl_usd,
                        outcome
                    FROM trades
                    WHERE strategy = $1
                      AND direction IN (
                            CASE WHEN $2 = 'UP' THEN 'YES' ELSE 'NO' END,
                            $2
                        )
                      AND outcome IN ('WIN', 'LOSS')
                      AND created_at >= NOW() - ($3 || ' seconds')::interval
                    ORDER BY created_at DESC
                    LIMIT 200
                    """,
                    cell.strategy_id,
                    cell.direction,
                    int(lookback_seconds),
                )
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.get_recent_trades_failed: %s",
                str(exc)[:200],
            )
            return []

        result: list[ResolvedTrade] = []
        for row in rows:
            eval_offset = row["eval_offset"]
            hour_utc = row["hour_utc"]
            # Filter to matching t_band and session.
            if _t_band(eval_offset) != cell.t_band:
                continue
            if _session(hour_utc) != cell.session:
                continue
            # Regime is optional — None matches any.
            row_regime = row["regime"]
            if cell.regime is not None and row_regime != cell.regime:
                continue

            is_win = row["outcome"] == "WIN"
            fp = row["fill_price"]
            result.append(
                ResolvedTrade(
                    strategy_id=cell.strategy_id,
                    direction=cell.direction,
                    eval_offset=eval_offset,
                    hour_utc=hour_utc,
                    regime=row_regime,
                    fill_price=float(fp) if fp is not None else None,
                    pnl_usd=float(row["pnl_usd"] or 0.0),
                    is_win=is_win,
                )
            )
        return result

    async def get_baseline_wr(
        self, cell: CellKey, lookback_seconds: int
    ) -> Optional[float]:
        """Return baseline WR over ``lookback_seconds`` (e.g. 7 days).

        Returns None when fewer than 10 resolved trades exist — not
        enough history to compute a stable baseline.

        B3 FIX: filter by the SAME cell axes (t_band, session, regime) so
        the baseline reflects THIS cell's history, not the strategy-wide
        average. Without this fix, a cell with naturally higher variance
        (e.g. us_pm × CASCADE) would compare against an average that
        includes easy cells, making Trigger 2 fire spuriously.
        """
        pool = self._get_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
                        COUNT(*) AS total
                    FROM trades
                    WHERE strategy = $1
                      AND direction IN (
                            CASE WHEN $2 = 'UP' THEN 'YES' ELSE 'NO' END,
                            $2
                        )
                      AND outcome IN ('WIN', 'LOSS')
                      AND created_at >= NOW() - ($3 || ' seconds')::interval
                      AND (metadata->>'eval_offset')::int IS NOT NULL
                      AND _session_label_matches(
                            EXTRACT(HOUR FROM created_at)::int, $4
                          )
                      AND _t_band_matches(
                            (metadata->>'eval_offset')::int, $5
                          )
                      AND ($6::text IS NULL
                           OR metadata->>'vpin_regime' = $6)
                    """,
                    cell.strategy_id,
                    cell.direction,
                    int(lookback_seconds),
                    cell.session,
                    cell.t_band,
                    cell.regime,
                )
        except Exception:
            # The _session_label_matches / _t_band_matches helpers don't exist
            # as SQL functions — fall back to in-Python filtering.
            return await self._get_baseline_wr_python(cell, lookback_seconds)

        if not row or row["total"] < 10:
            return None
        return float(row["wins"]) / float(row["total"])

    async def _get_baseline_wr_python(
        self, cell: CellKey, lookback_seconds: int
    ) -> Optional[float]:
        """Baseline WR with cell-axis filtering done in Python (no UDF needed).

        Fetches all strategy+direction trades then applies t_band/session/regime
        filters in-memory. Slightly more data transferred but correct without
        requiring custom SQL functions.
        """
        pool = self._get_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        outcome,
                        (metadata->>'eval_offset')::int   AS eval_offset,
                        EXTRACT(HOUR FROM created_at)::int AS hour_utc,
                        (metadata->>'vpin_regime')        AS regime
                    FROM trades
                    WHERE strategy = $1
                      AND direction IN (
                            CASE WHEN $2 = 'UP' THEN 'YES' ELSE 'NO' END,
                            $2
                        )
                      AND outcome IN ('WIN', 'LOSS')
                      AND created_at >= NOW() - ($3 || ' seconds')::interval
                      AND (metadata->>'eval_offset')::int IS NOT NULL
                    LIMIT 5000
                    """,
                    cell.strategy_id,
                    cell.direction,
                    int(lookback_seconds),
                )
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.get_baseline_wr_python_failed: %s",
                str(exc)[:200],
            )
            return None

        wins = 0
        total = 0
        for row in rows:
            # Filter to same cell axes.
            if _t_band(row["eval_offset"]) != cell.t_band:
                continue
            if _session(row["hour_utc"]) != cell.session:
                continue
            if cell.regime is not None and row["regime"] != cell.regime:
                continue
            total += 1
            if row["outcome"] == "WIN":
                wins += 1

        if total < 10:
            return None
        return float(wins) / float(total)

    async def is_cell_paused(self, cell: CellKey) -> bool:
        """Return True when an active (non-released, non-expired) pause
        exists for this cell. Fails open (returns False) on error."""
        pool = self._get_pool()
        if not pool:
            return False
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM cell_pauses
                    WHERE strategy_id = $1
                      AND direction   = $2
                      AND t_band      = $3
                      AND (regime     = $4 OR ($4 IS NULL AND regime IS NULL))
                      AND (session    = $5 OR ($5 IS NULL AND session IS NULL))
                      AND released_at IS NULL
                      AND pause_until > NOW()
                    LIMIT 1
                    """,
                    cell.strategy_id,
                    cell.direction,
                    cell.t_band,
                    cell.regime,
                    cell.session,
                )
                return row is not None
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.is_cell_paused_failed: %s",
                str(exc)[:200],
            )
            return False

    async def insert_pause(
        self,
        cell: CellKey,
        pause_seconds: int,
        reason: str,
        trigger_metric: dict[str, Any],
    ) -> Optional[int]:
        """Insert a pause row. Returns the new id, or None on collision
        (pause already in flight) or error."""
        pool = self._get_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO cell_pauses (
                        strategy_id, direction, t_band, regime, session,
                        pause_until, reason, trigger_metric
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        NOW() + ($6 || ' seconds')::interval,
                        $7, $8::jsonb
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    cell.strategy_id,
                    cell.direction,
                    cell.t_band,
                    cell.regime,
                    cell.session,
                    str(int(pause_seconds)),
                    reason[:500],
                    json.dumps(trigger_metric),
                )
                return int(row["id"]) if row else None
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.insert_pause_failed: %s",
                str(exc)[:200],
            )
            return None

    async def release_pause(
        self, pause_id: int, released_by: str
    ) -> None:
        """Set ``released_at = NOW()`` and ``released_by``."""
        pool = self._get_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE cell_pauses
                       SET released_at = NOW(),
                           released_by = $2
                     WHERE id = $1
                       AND released_at IS NULL
                    """,
                    int(pause_id),
                    released_by[:100],
                )
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.release_pause_failed: %s",
                str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # Extra: list_active_pauses (ops convenience, not required by Protocol)
    # ------------------------------------------------------------------

    async def list_active_pauses(
        self,
        strategy_id: Optional[str] = None,
        direction: Optional[str] = None,
        t_band: Optional[str] = None,
        regime: Optional[str] = None,
        session: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return all active pauses, optionally filtered.

        Used by the Hub ops dashboard and manual release flows.
        Returns dicts (not dataclasses) for easy JSON serialisation.
        """
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, strategy_id, direction, t_band, regime,
                           session, paused_at, pause_until, reason,
                           trigger_metric
                    FROM cell_pauses
                    WHERE released_at IS NULL
                      AND pause_until > NOW()
                      AND ($1::text IS NULL OR strategy_id = $1)
                      AND ($2::text IS NULL OR direction   = $2)
                      AND ($3::text IS NULL OR t_band      = $3)
                      AND ($4::text IS NULL OR regime      = $4)
                      AND ($5::text IS NULL OR session     = $5)
                    ORDER BY paused_at DESC
                    LIMIT 200
                    """,
                    strategy_id,
                    direction,
                    t_band,
                    regime,
                    session,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "pg_cell_pause_repo.list_active_pauses_failed: %s",
                str(exc)[:200],
            )
            return []
