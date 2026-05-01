"""PostgreSQL Window Repository -- per-aggregate persistence for window data.

Implements :class:`engine.domain.ports.WindowStateRepository` -- the single
owner of traded/resolved window state (CA-04, Phase 5).

Also handles all window_snapshots CRUD, shadow trade resolution, post-resolution
analysis, window predictions, and evaluation tick queries.

Delegates to the **exact same SQL** that ``engine/persistence/db_client.py``
uses today.  This is a thin structural split -- zero behaviour change.

Audit: CA-01 / CA-04 (Clean Architecture migration).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import httpx
import structlog

from domain.ports import WindowStateRepository
from domain.value_objects import WindowKey, WindowOutcome

log = structlog.get_logger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_SLUG_PREFIX = "btc-updown-5m-"


def _coerce_directional_outcome(outcome, poly_winner) -> Optional[str]:
    """Return UP/DOWN/FLAT or None — same semantics as
    ``DBClient._coerce_directional_outcome``. Module-level so the parity
    helper here uses the same logic without depending on DBClient.
    """
    if isinstance(poly_winner, str):
        up = poly_winner.strip().upper()
        if up in ("UP", "DOWN", "FLAT"):
            return up
    if isinstance(outcome, str):
        up = outcome.strip().upper()
        if up in ("UP", "DOWN", "FLAT"):
            return up
    return None


class PgWindowRepository(WindowStateRepository):
    """asyncpg-backed window snapshot repository.

    Implements :class:`WindowStateRepository` -- ``was_traded``,
    ``mark_traded``, ``was_resolved``, ``mark_resolved``, and
    ``load_recent_traded`` backed by the ``window_states`` table.

    Also retains all legacy window_snapshots / window_predictions
    methods for backward compatibility.

    Accepts an ``asyncpg.Pool`` -- the same pool the legacy ``DBClient``
    uses.  Methods copy SQL verbatim from ``db_client.py`` so behaviour
    parity is byte-for-byte.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- Table Setup --------------------------------------------------------

    async def ensure_window_tables(self) -> None:
        """Create window_snapshots table if it doesn't exist.

        Verbatim SQL from ``DBClient.ensure_window_tables``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS window_snapshots (
                        id SERIAL PRIMARY KEY,
                        window_ts BIGINT NOT NULL,
                        asset VARCHAR(10) NOT NULL,
                        timeframe VARCHAR(5) NOT NULL,
                        open_price DOUBLE PRECISION,
                        close_price DOUBLE PRECISION,
                        delta_pct DOUBLE PRECISION,
                        vpin DOUBLE PRECISION,
                        regime VARCHAR(20),
                        cg_connected BOOLEAN DEFAULT FALSE,
                        cg_oi_usd DOUBLE PRECISION,
                        cg_oi_delta_pct DOUBLE PRECISION,
                        cg_liq_long_usd DOUBLE PRECISION,
                        cg_liq_short_usd DOUBLE PRECISION,
                        cg_liq_total_usd DOUBLE PRECISION,
                        cg_long_pct DOUBLE PRECISION,
                        cg_short_pct DOUBLE PRECISION,
                        cg_long_short_ratio DOUBLE PRECISION,
                        cg_top_long_pct DOUBLE PRECISION,
                        cg_top_short_pct DOUBLE PRECISION,
                        cg_top_ratio DOUBLE PRECISION,
                        cg_taker_buy_usd DOUBLE PRECISION,
                        cg_taker_sell_usd DOUBLE PRECISION,
                        cg_funding_rate DOUBLE PRECISION,
                        direction VARCHAR(4),
                        confidence DOUBLE PRECISION,
                        cg_modifier DOUBLE PRECISION,
                        trade_placed BOOLEAN DEFAULT FALSE,
                        skip_reason VARCHAR(100),
                        outcome VARCHAR(4),
                        pnl_usd DOUBLE PRECISION,
                        poly_winner VARCHAR(10),
                        btc_price DOUBLE PRECISION,
                        -- TWAP data (v5.7)
                        twap_delta_pct DOUBLE PRECISION,
                        twap_direction VARCHAR(4),
                        twap_gamma_agree BOOLEAN,
                        twap_agreement_score INTEGER,
                        twap_confidence_boost DOUBLE PRECISION,
                        twap_n_ticks INTEGER,
                        twap_stability DOUBLE PRECISION,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(window_ts, asset, timeframe)
                    );
                    CREATE INDEX IF NOT EXISTS idx_ws_ts ON window_snapshots(window_ts);
                    CREATE INDEX IF NOT EXISTS idx_ws_regime ON window_snapshots(regime);
                """)
                # Safe migration: add TWAP columns if table already exists (v5.7)
                for col, col_type in [
                    ("twap_delta_pct", "DOUBLE PRECISION"),
                    ("twap_direction", "VARCHAR(4)"),
                    ("twap_gamma_agree", "BOOLEAN"),
                    ("twap_agreement_score", "INTEGER"),
                    ("twap_confidence_boost", "DOUBLE PRECISION"),
                    ("twap_n_ticks", "INTEGER"),
                    ("twap_stability", "DOUBLE PRECISION"),
                    # v5.7c: trend + momentum + gamma gate
                    ("twap_trend_pct", "DOUBLE PRECISION"),
                    ("twap_momentum_pct", "DOUBLE PRECISION"),
                    ("twap_gamma_gate", "VARCHAR(12)"),
                    ("twap_should_skip", "BOOLEAN"),
                    ("twap_skip_reason", "VARCHAR(200)"),
                    # v6.0: TimesFM forecast data
                    ("timesfm_direction", "VARCHAR(4)"),
                    ("timesfm_confidence", "DOUBLE PRECISION"),
                    ("timesfm_predicted_close", "DOUBLE PRECISION"),
                    ("timesfm_delta_vs_open", "DOUBLE PRECISION"),
                    ("timesfm_spread", "DOUBLE PRECISION"),
                    ("timesfm_p10", "DOUBLE PRECISION"),
                    ("timesfm_p50", "DOUBLE PRECISION"),
                    ("timesfm_p90", "DOUBLE PRECISION"),
                    # v6.0: Spread/liquidity data
                    ("market_best_bid", "DOUBLE PRECISION"),
                    ("market_best_ask", "DOUBLE PRECISION"),
                    ("market_spread", "DOUBLE PRECISION"),
                    ("market_mid_price", "DOUBLE PRECISION"),
                    ("market_volume", "DOUBLE PRECISION"),
                    ("market_liquidity", "DOUBLE PRECISION"),
                    # v8.0: engine metadata + gate audit + shadow trade tracking
                    ("engine_version", "VARCHAR(10)"),
                    ("delta_source", "VARCHAR(10)"),
                    ("confidence_tier", "VARCHAR(10)"),
                    ("gates_passed", "TEXT"),
                    ("gate_failed", "VARCHAR(20)"),
                    ("shadow_trade_direction", "VARCHAR(4)"),
                    ("shadow_trade_entry_price", "DOUBLE PRECISION"),
                    # v4.4.0 (2026-04-16): denormalise v3/v4 surface fields so
                    # analysts can do fast SQL without extracting from the
                    # window_evaluation_traces.surface_json JSONB. These were
                    # defined in other migrations but never populated by the
                    # writer — this commit wires them up end-to-end.
                    ("sub_signal_elm", "DOUBLE PRECISION"),
                    ("sub_signal_cascade", "DOUBLE PRECISION"),
                    ("sub_signal_taker", "DOUBLE PRECISION"),
                    ("sub_signal_vpin", "DOUBLE PRECISION"),
                    ("sub_signal_momentum", "DOUBLE PRECISION"),
                    ("sub_signal_oi", "DOUBLE PRECISION"),
                    ("sub_signal_funding", "DOUBLE PRECISION"),
                    ("regime_confidence", "DOUBLE PRECISION"),
                    ("regime_persistence", "DOUBLE PRECISION"),
                    ("strategy_conviction", "VARCHAR(10)"),
                    ("strategy_conviction_score", "DOUBLE PRECISION"),
                    ("consensus_safe_to_trade", "BOOLEAN"),
                    ("consensus_agreement_score", "DOUBLE PRECISION"),
                    ("consensus_divergence_bps", "DOUBLE PRECISION"),
                    ("macro_bias", "VARCHAR(10)"),
                    ("macro_direction_gate", "VARCHAR(12)"),
                    ("macro_size_modifier", "DOUBLE PRECISION"),
                ]:
                    try:
                        await conn.execute(
                            f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                        )
                    except Exception:
                        pass  # Column already exists or not supported
            log.info("db.window_tables_ensured")
        except Exception as exc:
            log.error("db.ensure_window_tables_failed", error=str(exc))

    # -- Window Snapshot Updates ---------------------------------------------

    async def update_window_outcome(
        self,
        window_ts,
        asset: str,
        timeframe: str,
        outcome: str,
        pnl_usd: float,
        poly_winner=None,
    ) -> None:
        """Update a window_snapshot with resolution data.

        Mirrors ``DBClient.update_window_outcome`` (2026-04-30 forward-writer
        regression fix): coerces ``outcome`` to UP/DOWN/FLAT using
        ``poly_winner`` as source of truth, never overwrites a non-NULL
        value, and surfaces failures in the log.
        """
        if not self._pool:
            return

        directional = _coerce_directional_outcome(outcome, poly_winner)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                       SET outcome     = COALESCE(outcome, $1),
                           pnl_usd     = COALESCE(pnl_usd, $2),
                           poly_winner = COALESCE(poly_winner, $3)
                    WHERE window_ts = $4 AND asset = $5 AND timeframe = $6
                    """,
                    directional,
                    pnl_usd,
                    poly_winner,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.window_outcome_updated",
                window_ts=window_ts,
                asset=asset,
                timeframe=timeframe,
                outcome=directional,
                pnl_usd=pnl_usd,
                poly_winner=poly_winner,
                input_outcome=outcome,
            )
        except Exception as exc:
            log.error(
                "db.update_window_outcome_failed",
                error=str(exc),
                window_ts=window_ts,
                asset=asset,
                timeframe=timeframe,
            )

    async def update_signal_evaluations_outcome(
        self,
        window_ts,
        asset: str,
        timeframe: str,
        outcome: str,
    ) -> int:
        """Bulk-update every ``signal_evaluations`` row for a window with the
        resolved direction. Mirrors ``DBClient.update_signal_evaluations_outcome``.

        Added 2026-04-30 forward-writer regression fix: previously no engine
        path ever populated this column. Returns rows affected. Idempotent.
        """
        if not self._pool:
            return 0

        directional = _coerce_directional_outcome(outcome, None)
        if directional is None:
            return 0

        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE signal_evaluations
                       SET outcome = $1
                    WHERE window_ts = $2 AND asset = $3 AND timeframe = $4
                      AND outcome IS NULL
                    """,
                    directional,
                    window_ts,
                    asset,
                    timeframe,
                )
            n = int(result.split()[-1]) if result else 0
            log.debug(
                "db.signal_evaluations_outcome_updated",
                window_ts=window_ts,
                asset=asset,
                timeframe=timeframe,
                outcome=directional,
                rows=n,
            )
            return n
        except Exception as exc:
            log.error(
                "db.update_signal_evaluations_outcome_failed",
                error=str(exc),
                window_ts=window_ts,
                asset=asset,
            )
            return 0

    async def update_window_prices(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        **kwargs,
    ) -> None:
        """Update price columns on window_snapshot.

        Verbatim SQL from ``DBClient.update_window_prices``.
        """
        if not self._pool:
            return
        valid_cols = {
            "chainlink_open",
            "chainlink_close",
            "tiingo_open",
            "tiingo_close",
            "poly_resolved_outcome",
            "poly_up_price_final",
            "poly_down_price_final",
        }
        updates = []
        params = []
        idx = 4  # $1=window_ts, $2=asset, $3=timeframe
        for col, val in kwargs.items():
            if col in valid_cols and val is not None:
                idx += 1
                updates.append(f"{col} = ${idx}")
                params.append(val)
        if not updates:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE window_snapshots SET {', '.join(updates)} "
                    f"WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                    *params,
                )
        except Exception as exc:
            log.error("db.update_window_prices_failed", error=str(exc)[:80])

    async def update_window_resolution_extras(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        binance_close: Optional[float] = None,
        chainlink_binance_direction_match: Optional[bool] = None,
        resolution_delay_secs: Optional[int] = None,
    ) -> None:
        """Update extra resolution columns (v7.2).

        Verbatim SQL from ``DBClient.update_window_resolution_extras``.
        """
        if not self._pool:
            return
        updates = []
        params = []
        idx = 4
        if binance_close is not None:
            idx += 1
            updates.append(f"binance_close = ${idx}")
            params.append(binance_close)
        if chainlink_binance_direction_match is not None:
            idx += 1
            updates.append(f"chainlink_binance_direction_match = ${idx}")
            params.append(chainlink_binance_direction_match)
        if resolution_delay_secs is not None:
            idx += 1
            updates.append(f"resolution_delay_secs = ${idx}")
            params.append(resolution_delay_secs)
        if not updates:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE window_snapshots SET {', '.join(updates)} "
                    f"WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                    *params,
                )
        except Exception as exc:
            log.error("db.update_window_resolution_extras_failed", error=str(exc)[:80])

    async def update_gamma_prices(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        gamma_up: float,
        gamma_down: float,
    ) -> None:
        """Store fresh T-60 Gamma prices to window_snapshot.

        Verbatim SQL from ``DBClient.update_gamma_prices``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET gamma_up_price = $1, gamma_down_price = $2 WHERE window_ts = $3 AND asset = $4 AND timeframe = $5",
                    gamma_up,
                    gamma_down,
                    window_ts,
                    asset,
                    timeframe,
                )
        except Exception:
            pass

    async def get_window_close(
        self, window_ts: int, asset: str, timeframe: str
    ) -> float:
        """Get the close price for a resolved window from window_snapshots.

        Verbatim SQL from ``DBClient.get_window_close``.
        """
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT close_price FROM window_snapshots WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                )
                return float(row) if row else 0.0
        except Exception:
            return 0.0

    async def update_window_trade_placed(
        self, window_ts: int, asset: str, timeframe: str
    ) -> None:
        """Mark a window_snapshot as having a trade placed.

        Verbatim SQL from ``DBClient.update_window_trade_placed``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE window_snapshots SET trade_placed = TRUE WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                )
                log.info(
                    "db.trade_placed_updated",
                    window_ts=window_ts,
                    asset=asset,
                    result=result,
                )
        except Exception as exc:
            log.error(
                "db.trade_placed_update_failed",
                window_ts=window_ts,
                asset=asset,
                error=str(exc),
            )

    async def update_window_fok_data(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        execution_mode: str,
        fok_attempts: int,
        fok_fill_step: int,
        clob_fill_price: float,
    ) -> None:
        """Write FOK execution details to window_snapshot after a successful fill.

        Verbatim SQL from ``DBClient.update_window_fok_data``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE window_snapshots
                       SET execution_mode = $4, fok_attempts = $5,
                           fok_fill_step = $6, clob_fill_price = $7
                       WHERE window_ts = $1 AND asset = $2 AND timeframe = $3""",
                    window_ts,
                    asset,
                    timeframe,
                    execution_mode,
                    fok_attempts,
                    fok_fill_step,
                    clob_fill_price,
                )
        except Exception as exc:
            log.error("db.fok_data_update_failed", window_ts=window_ts, error=str(exc))

    async def update_window_skip_reason(
        self, window_ts: int, asset: str, timeframe: str, skip_reason: str
    ) -> None:
        """Update skip_reason on a window_snapshot after evaluation.

        Verbatim SQL from ``DBClient.update_window_skip_reason``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET skip_reason = $1 WHERE window_ts = $2 AND asset = $3 AND timeframe = $4",
                    skip_reason,
                    window_ts,
                    asset,
                    timeframe,
                )
        except Exception:
            pass

    # -- Shadow Trade Resolution ---------------------------------------------

    async def ensure_shadow_columns(self) -> None:
        """Add shadow trade resolution columns to window_snapshots if missing.

        Verbatim SQL from ``DBClient.ensure_shadow_columns``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for col, col_type in [
                    ("shadow_trade_direction", "VARCHAR(4)"),
                    ("shadow_trade_entry_price", "DOUBLE PRECISION"),
                    ("oracle_outcome", "VARCHAR(4)"),
                    ("shadow_pnl", "DOUBLE PRECISION"),
                    ("shadow_would_win", "BOOLEAN"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
            log.info("db.shadow_columns_ensured")
        except Exception as exc:
            log.warning("db.ensure_shadow_columns_failed", error=str(exc))

    async def get_unresolved_shadow_windows(self, minutes_back: int = 10) -> list:
        """Get recent skipped windows that haven't been shadow-resolved yet.

        Verbatim SQL from ``DBClient.get_unresolved_shadow_windows``.
        """
        if not self._pool:
            return []
        try:
            cutoff_ts = int(__import__("time").time()) - (minutes_back * 60)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT window_ts, asset, timeframe,
                           shadow_trade_direction, shadow_trade_entry_price,
                           skip_reason, confidence
                    FROM window_snapshots
                    WHERE trade_placed = FALSE
                      AND shadow_trade_direction IS NOT NULL
                      AND oracle_outcome IS NULL
                      AND window_ts > $1
                    ORDER BY window_ts DESC
                    LIMIT 20
                    """,
                    cutoff_ts,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("db.get_unresolved_shadow_windows_failed", error=str(exc))
            return []

    async def update_shadow_resolution(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        oracle_outcome: str,
        shadow_pnl: float,
        shadow_would_win: bool,
    ) -> None:
        """Update a skipped window with oracle resolution for shadow trade analysis.

        Mirrors ``DBClient.update_shadow_resolution`` (2026-04-30 forward-writer
        fix): also populates the canonical ``outcome`` column.
        """
        if not self._pool:
            return
        directional = _coerce_directional_outcome(oracle_outcome, None)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                       SET oracle_outcome   = COALESCE(oracle_outcome, $1),
                           outcome          = COALESCE(outcome, $4),
                           shadow_pnl       = COALESCE(shadow_pnl, $2),
                           shadow_would_win = COALESCE(shadow_would_win, $3)
                    WHERE window_ts = $5 AND asset = $6 AND timeframe = $7
                    """,
                    oracle_outcome,
                    shadow_pnl,
                    shadow_would_win,
                    directional,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.shadow_resolution_updated",
                window_ts=window_ts,
                asset=asset,
                oracle_outcome=oracle_outcome,
                outcome=directional,
                shadow_pnl=f"{shadow_pnl:+.2f}",
                shadow_would_win=shadow_would_win,
            )
        except Exception as exc:
            log.error(
                "db.update_shadow_resolution_failed",
                error=str(exc),
                window_ts=window_ts,
                asset=asset,
            )

    # -- Post-Resolution AI Analysis -----------------------------------------

    async def ensure_post_resolution_table(self) -> None:
        """Ensure post_resolution_analyses table exists (idempotent).

        Verbatim SQL from ``DBClient.ensure_post_resolution_table``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS post_resolution_analyses (
                        id                SERIAL PRIMARY KEY,
                        window_ts         BIGINT NOT NULL,
                        asset             VARCHAR(10) NOT NULL DEFAULT 'BTC',
                        timeframe         VARCHAR(5)  NOT NULL DEFAULT '5m',
                        oracle_direction  VARCHAR(4),
                        n_ticks           INTEGER DEFAULT 0,
                        missed_profit_usd DOUBLE PRECISION DEFAULT 0,
                        blocked_loss_usd  DOUBLE PRECISION DEFAULT 0,
                        cap_too_tight     BOOLEAN DEFAULT FALSE,
                        gate_recommendation TEXT,
                        ai_post_analysis  TEXT,
                        analysed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (window_ts, asset, timeframe)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pra_window_ts ON post_resolution_analyses(window_ts)"
                )
                # Also add summary columns to window_snapshots for quick access
                for col, col_type in [
                    ("ai_post_analysis", "TEXT"),
                    ("missed_profit_usd", "DOUBLE PRECISION"),
                    ("blocked_loss_usd", "DOUBLE PRECISION"),
                    ("cap_too_tight", "BOOLEAN"),
                    ("gate_recommendation", "TEXT"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
            log.info("db.post_resolution_table_ensured")
        except Exception as exc:
            log.warning("db.ensure_post_resolution_table_failed", error=str(exc))

    async def store_post_resolution_analysis(self, result: dict) -> None:
        """Persist post-resolution AI analysis to DB.

        Verbatim SQL from ``DBClient.store_post_resolution_analysis``.
        """
        if not self._pool:
            return
        try:
            window_ts = int(result["window_ts"])
            asset = result.get("asset", "BTC")
            timeframe = result.get("timeframe", "5m")
            oracle_direction = result.get("oracle_direction")
            n_ticks = int(result.get("n_ticks", 0))
            missed_profit = float(result.get("missed_profit_usd", 0.0))
            blocked_loss = float(result.get("blocked_loss_usd", 0.0))
            cap_too_tight = bool(result.get("cap_too_tight", False))
            gate_rec = result.get("gate_recommendation")
            ai_text = result.get("ai_post_analysis", "")

            async with self._pool.acquire() as conn:
                # Upsert into post_resolution_analyses
                await conn.execute(
                    """
                    INSERT INTO post_resolution_analyses (
                        window_ts, asset, timeframe,
                        oracle_direction, n_ticks,
                        missed_profit_usd, blocked_loss_usd,
                        cap_too_tight, gate_recommendation, ai_post_analysis
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (window_ts, asset, timeframe) DO UPDATE SET
                        oracle_direction  = EXCLUDED.oracle_direction,
                        n_ticks           = EXCLUDED.n_ticks,
                        missed_profit_usd = EXCLUDED.missed_profit_usd,
                        blocked_loss_usd  = EXCLUDED.blocked_loss_usd,
                        cap_too_tight     = EXCLUDED.cap_too_tight,
                        gate_recommendation = EXCLUDED.gate_recommendation,
                        ai_post_analysis  = EXCLUDED.ai_post_analysis,
                        analysed_at       = NOW()
                    """,
                    window_ts,
                    asset,
                    timeframe,
                    oracle_direction,
                    n_ticks,
                    missed_profit,
                    blocked_loss,
                    cap_too_tight,
                    gate_rec,
                    ai_text[:4000] if ai_text else None,
                )
                # Update summary columns in window_snapshots
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET ai_post_analysis  = $1,
                        missed_profit_usd = $2,
                        blocked_loss_usd  = $3,
                        cap_too_tight     = $4,
                        gate_recommendation = $5
                    WHERE window_ts = $6 AND asset = $7 AND timeframe = $8
                    """,
                    ai_text[:4000] if ai_text else None,
                    missed_profit,
                    blocked_loss,
                    cap_too_tight,
                    gate_rec,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.post_resolution_stored",
                window_ts=window_ts,
                missed=f"+${missed_profit:.2f}",
                avoided=f"-${blocked_loss:.2f}",
                cap_too_tight=cap_too_tight,
            )
        except Exception as exc:
            log.warning("db.store_post_resolution_failed", error=str(exc)[:120])

    async def get_eval_ticks_for_window(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
    ) -> list:
        """Fetch all evaluation ticks for a window from gate_check_traces.

        Supersedes the legacy gate_audit read path.  Returns one dict per
        (eval_offset, gate_order) row shaped to match the downstream
        post_resolution_evaluator contract.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        eval_offset        AS offset,
                        skip_reason,
                        passed             AS gate_passed,
                        reason             AS gate_failed,
                        action             AS decision,
                        observed_json->>'vpin'      AS vpin,
                        observed_json->>'delta_pct' AS delta_pct,
                        observed_json->>'regime'    AS regime
                    FROM gate_check_traces
                    WHERE window_ts = $1
                      AND asset     = $2
                      AND timeframe = $3
                    ORDER BY eval_offset DESC NULLS LAST, gate_order ASC
                    """,
                    window_ts,
                    asset,
                    timeframe,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.debug("db.get_eval_ticks_failed", error=str(exc)[:80])
            return []

    # -- Window Predictions --------------------------------------------------

    async def ensure_window_predictions_table(self) -> None:
        """Create window_predictions table for tracking predicted vs actual outcomes.

        Verbatim SQL from ``DBClient.ensure_window_predictions_table``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS window_predictions (
                        window_ts         BIGINT NOT NULL,
                        asset             VARCHAR(10) NOT NULL DEFAULT 'BTC',
                        timeframe         VARCHAR(5)  NOT NULL DEFAULT '5m',
                        tiingo_open       DOUBLE PRECISION,
                        tiingo_close      DOUBLE PRECISION,
                        chainlink_open    DOUBLE PRECISION,
                        chainlink_close   DOUBLE PRECISION,
                        tiingo_direction  VARCHAR(4),
                        chainlink_direction VARCHAR(4),
                        our_signal_direction VARCHAR(4),
                        v2_direction      VARCHAR(4),
                        v2_probability    DOUBLE PRECISION,
                        vpin_at_close     DOUBLE PRECISION,
                        regime            VARCHAR(15),
                        trade_placed      BOOLEAN DEFAULT FALSE,
                        our_direction     VARCHAR(4),
                        our_entry_price   DOUBLE PRECISION,
                        bid_unfilled      BOOLEAN DEFAULT FALSE,
                        skip_reason       TEXT,
                        oracle_winner     VARCHAR(4),
                        tiingo_correct    BOOLEAN,
                        chainlink_correct BOOLEAN,
                        our_signal_correct BOOLEAN,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (window_ts, asset, timeframe)
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_wp_window_ts ON window_predictions(window_ts)"
                )
            log.info("db.window_predictions_table_ensured")
        except Exception as exc:
            log.warning("db.ensure_window_predictions_failed", error=str(exc))

    async def write_window_prediction(self, data: dict) -> None:
        """Write or update a window prediction record.

        Verbatim SQL from ``DBClient.write_window_prediction``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_predictions (
                        window_ts, asset, timeframe,
                        tiingo_open, tiingo_close,
                        chainlink_open, chainlink_close,
                        tiingo_direction, chainlink_direction,
                        our_signal_direction, v2_direction, v2_probability,
                        vpin_at_close, regime,
                        trade_placed, our_direction, our_entry_price,
                        bid_unfilled, skip_reason
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                    ON CONFLICT (window_ts, asset, timeframe) DO UPDATE SET
                        tiingo_close = EXCLUDED.tiingo_close,
                        chainlink_close = EXCLUDED.chainlink_close,
                        tiingo_direction = EXCLUDED.tiingo_direction,
                        chainlink_direction = EXCLUDED.chainlink_direction,
                        our_signal_direction = EXCLUDED.our_signal_direction,
                        v2_direction = EXCLUDED.v2_direction,
                        v2_probability = EXCLUDED.v2_probability,
                        vpin_at_close = EXCLUDED.vpin_at_close,
                        regime = EXCLUDED.regime,
                        trade_placed = EXCLUDED.trade_placed,
                        our_direction = EXCLUDED.our_direction,
                        our_entry_price = EXCLUDED.our_entry_price,
                        bid_unfilled = EXCLUDED.bid_unfilled,
                        skip_reason = EXCLUDED.skip_reason
                """,
                    int(data.get("window_ts", 0)),
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    data.get("tiingo_open"),
                    data.get("tiingo_close"),
                    data.get("chainlink_open"),
                    data.get("chainlink_close"),
                    data.get("tiingo_direction"),
                    data.get("chainlink_direction"),
                    data.get("our_signal_direction"),
                    data.get("v2_direction"),
                    data.get("v2_probability"),
                    data.get("vpin_at_close"),
                    data.get("regime"),
                    data.get("trade_placed", False),
                    data.get("our_direction"),
                    data.get("our_entry_price"),
                    data.get("bid_unfilled", False),
                    data.get("skip_reason"),
                )
        except Exception as exc:
            log.warning("db.write_window_prediction_failed", error=str(exc)[:120])

    async def update_window_prediction_outcome(
        self, window_ts: int, asset: str, oracle_winner: str
    ) -> None:
        """After oracle resolution, update the prediction with actual outcome.

        Verbatim SQL from ``DBClient.update_window_prediction_outcome``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_predictions SET
                        oracle_winner = $4::varchar,
                        tiingo_correct = (tiingo_direction = $4::varchar),
                        chainlink_correct = (chainlink_direction = $4::varchar),
                        our_signal_correct = (our_signal_direction = $4::varchar)
                    WHERE window_ts = $1 AND asset = $2 AND timeframe = $3
                """,
                    window_ts,
                    asset,
                    "5m",
                    oracle_winner.upper(),
                )
        except Exception as exc:
            log.warning("db.update_prediction_outcome_failed", error=str(exc)[:120])

    # ======================================================================
    # WindowStateRepository port methods (stubs -- Phase 2)
    # ======================================================================
    # WindowStateRepository implementation (CA-04, Phase 5)
    # Single owner of traded/resolved state -- replaces triple in-memory sets
    # ======================================================================

    async def ensure_window_states_table(self) -> None:
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""CREATE TABLE IF NOT EXISTS window_states (
                    id SERIAL PRIMARY KEY, asset VARCHAR(10) NOT NULL,
                    window_ts BIGINT NOT NULL, timeframe VARCHAR(10) NOT NULL DEFAULT '5m',
                    order_id TEXT, traded_at TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ, outcome VARCHAR(10),
                    actual_direction VARCHAR(10),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (asset, window_ts, timeframe))""")
                await conn.execute("""CREATE INDEX IF NOT EXISTS idx_window_states_traded_at
                    ON window_states (traded_at) WHERE traded_at IS NOT NULL""")
                await conn.execute("""CREATE INDEX IF NOT EXISTS idx_window_states_resolved_at
                    ON window_states (resolved_at) WHERE resolved_at IS NOT NULL""")
                # window_claims — lease-based dedup (audit #316). Separate from
                # window_states so claims (transient leases) and fills (terminal
                # records) don't share the dual-meaning of order_id='pending'.
                #
                # PK includes strategy_id (audit #320, 2026-04-26): without
                # strategy_id, sibling strategies (v9_lgb_only, v9_ensemble,
                # v10_lgb_only, …) all fight for the SAME row on every eval
                # tick. The first to acquire holds the lease for 15s,
                # blocking ALL siblings (dedup_hit cascade), even though
                # different strategies should be allowed to take independent
                # positions on the same window. With strategy_id in the PK
                # each strategy has its own row → no cross-strategy contention.
                await conn.execute("""CREATE TABLE IF NOT EXISTS window_claims (
                    asset VARCHAR(10) NOT NULL,
                    window_ts BIGINT NOT NULL,
                    timeframe VARCHAR(10) NOT NULL DEFAULT '5m',
                    strategy_id TEXT NOT NULL DEFAULT '',
                    claim_id UUID NOT NULL,
                    claimed_by TEXT,
                    claimed_at TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    attempt_n INT NOT NULL DEFAULT 1,
                    PRIMARY KEY (asset, window_ts, timeframe, strategy_id))""")
                # ── Idempotent migration for existing installs (audit #320) ──
                # Pre-#320 PK was (asset, window_ts, timeframe). The next 4
                # statements are no-ops on fresh installs (CREATE TABLE above
                # already includes strategy_id + new PK) and the migration
                # path on existing DBs:
                #   1. Add strategy_id column with a sentinel default for any
                #      orphan rows (those will TTL out within 15s).
                #   2. Drop the old PK constraint if present.
                #   3. Add the new PK including strategy_id.
                # Each statement is guarded so re-running ensure() is safe.
                await conn.execute(
                    """ALTER TABLE window_claims
                        ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT ''"""
                )
                await conn.execute(
                    """DO $$
                        DECLARE
                            pk_cols TEXT;
                        BEGIN
                            SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
                                INTO pk_cols
                                FROM pg_constraint c
                                JOIN pg_attribute a
                                  ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                                WHERE c.conrelid = 'window_claims'::regclass
                                  AND c.contype = 'p';
                            IF pk_cols IS NOT NULL AND pk_cols !~ 'strategy_id' THEN
                                EXECUTE 'ALTER TABLE window_claims DROP CONSTRAINT '
                                    || (SELECT conname FROM pg_constraint
                                        WHERE conrelid = 'window_claims'::regclass
                                          AND contype = 'p');
                                EXECUTE 'ALTER TABLE window_claims ADD PRIMARY KEY '
                                    || '(asset, window_ts, timeframe, strategy_id)';
                            END IF;
                        END $$"""
                )
                await conn.execute("""CREATE INDEX IF NOT EXISTS idx_window_claims_expires_at
                    ON window_claims (expires_at)""")
                # ── strategy_window_fills (audit #321, 2026-04-26) ──────────
                # Per-strategy terminal fill marker. Backs has_filled() so
                # the same strategy cannot fill the same window twice.
                #
                # Why a new table (not a column on window_states): the
                # existing UNIQUE (asset, window_ts, timeframe) constraint
                # restricts window_states to one row per window. Sibling
                # strategies (v9_lgb_only + v10_lgb_only) BOTH legitimately
                # fill the same window — two rows. Splitting the marker out
                # avoids breaking the existing window_states schema and
                # leaves the resolved_at / outcome columns alone.
                #
                # Smoking gun this fixes: 2026-04-26 v10_lgb_only filled 3x
                # on window 1777234800 (20:21:12, 20:21:40, 20:22:11). The
                # lease (#320) released on each fill, letting the same
                # strategy re-acquire and re-fill seconds later.
                await conn.execute("""CREATE TABLE IF NOT EXISTS strategy_window_fills (
                    asset VARCHAR(10) NOT NULL,
                    window_ts BIGINT NOT NULL,
                    timeframe VARCHAR(10) NOT NULL DEFAULT '5m',
                    strategy_id TEXT NOT NULL,
                    order_id TEXT,
                    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (asset, window_ts, timeframe, strategy_id))""")
                await conn.execute("""CREATE INDEX IF NOT EXISTS idx_strategy_window_fills_filled_at
                    ON strategy_window_fills (filled_at)""")
            log.info("db.window_states_table_ensured")
        except Exception as exc:
            log.error("db.ensure_window_states_table_failed", error=str(exc)[:200])

    async def was_traded(self, key: WindowKey) -> bool:
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM window_states WHERE asset = $1 AND window_ts = $2 AND timeframe = $3 AND order_id IS NOT NULL)",
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                )
                return bool(row)
        except Exception as exc:
            log.warning("db.was_traded_failed", key=str(key), error=str(exc)[:120])
            return False

    async def mark_traded(
        self,
        key: WindowKey,
        order_id: str,
        strategy_id: Optional[str] = None,
    ) -> None:
        """Record a fill on the given window.

        Audit #321 (2026-04-26): when ``strategy_id`` is provided we ALSO
        write to ``strategy_window_fills`` so :meth:`has_filled` can
        return True for subsequent attempts. Without this row, a sibling
        strategy filling the same window could not be distinguished
        from a same-strategy double-fill.

        ``strategy_id=None`` is supported for backfill compatibility —
        the legacy ``window_states`` row is still written, but no
        strategy-fill marker. New callsites SHOULD always pass
        ``strategy_id``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                now = datetime.now(timezone.utc)
                await conn.execute(
                    """
                    INSERT INTO window_states (asset, window_ts, timeframe, traded_at, order_id)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (asset, window_ts, timeframe) DO UPDATE
                        SET order_id = EXCLUDED.order_id
                        WHERE window_states.order_id IS NULL
                           OR window_states.order_id = 'pending'
                """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    now,
                    order_id,
                )
                if strategy_id:
                    # Audit #322 (2026-04-26): when a pessimistic claim
                    # was placed via try_claim_fill_slot the row already
                    # exists with order_id = 'pending'. UPSERT to stamp
                    # the real order_id; without DO UPDATE the row
                    # would stay as the placeholder forever.
                    await conn.execute(
                        """
                        INSERT INTO strategy_window_fills
                            (asset, window_ts, timeframe, strategy_id, order_id, filled_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (asset, window_ts, timeframe, strategy_id) DO UPDATE
                            SET order_id = EXCLUDED.order_id,
                                filled_at = EXCLUDED.filled_at
                            WHERE strategy_window_fills.order_id = $7
                               OR strategy_window_fills.order_id IS NULL
                        """,
                        key.asset,
                        key.window_ts,
                        key.timeframe,
                        strategy_id,
                        order_id,
                        now,
                        self.PLACEHOLDER_ORDER_ID,
                    )
            log.debug(
                "db.mark_traded",
                key=str(key),
                order_id=order_id[:20] if order_id else None,
                strategy=strategy_id,
            )
        except Exception as exc:
            log.warning("db.mark_traded_failed", key=str(key), error=str(exc)[:120])

    async def has_filled(self, key: WindowKey, strategy_id: str) -> bool:
        """Return True if ``strategy_id`` already filled this window.

        Audit #321 (2026-04-26): primary "once-per-window-per-strategy"
        invariant. Queried BEFORE lease acquisition in execute_trade so
        repeat attempts on the same (window, strategy) short-circuit
        without ever touching window_claims.

        Returns False on any DB error or pool absence — fail-open keeps
        the engine running when the marker table is unreachable; the
        lease still provides 15s in-flight protection as a backstop.

        Audit #401 (2026-04-28): stale-pending self-recovery. SMOKING
        GUN today: 52 rows with order_id='pending' lingered in
        strategy_window_fills, blocking v9_lgb_only and v10_lgb_only
        entries forever until manual DELETE. Root cause: every release
        path was bounded by DB_AWAIT_TIMEOUT_S, which under DB-pool
        saturation can elapse without the DELETE actually committing.
        ``slot_released`` stays False, the finally backstop's redundant
        retry hits the same saturated pool, and the placeholder lives on.
        try_claim_fill_slot's stale-takeover (TTL — see
        STALE_PLACEHOLDER_TTL_SECONDS) self-heals on the
        NEXT attempt — but only if execute_trade reaches that step.
        Today's failure was that has_filled SHORT-CIRCUITS earlier with
        a True for the leaked 'pending' row, and the strategy never
        gets to try_claim_fill_slot's takeover.

        Defence: ignore 'pending' placeholders older than the same
        ``STALE_PLACEHOLDER_TTL_SECONDS`` window. Real fills (order_id
        != 'pending') still block as terminal markers regardless of
        age. This makes the per-window dedup self-recovering without
        any cleanup cron — a leaked 'pending' row blocks for at most
        TTL seconds, then the next eval tick passes has_filled,
        try_claim_fill_slot's stale-takeover steals the row, and the
        strategy resumes normal operation.
        """
        if not self._pool:
            return False
        if not strategy_id:
            return False
        try:
            async with self._pool.acquire() as conn:
                # EXISTS query short-circuits at the row level. The
                # WHERE predicate matches:
                #   * Any real fill marker (order_id != 'pending'), OR
                #   * An ACTIVE 'pending' placeholder (filled_at within
                #     the TTL window — another in-flight attempt holds
                #     the slot, we should NOT race it).
                # Stale 'pending' rows (older than TTL) are excluded —
                # the owning attempt is dead, has_filled returns False,
                # and the next try_claim_fill_slot's stale-takeover
                # will reclaim the row.
                row = await conn.fetchval(
                    """SELECT EXISTS(
                        SELECT 1 FROM strategy_window_fills
                         WHERE asset = $1 AND window_ts = $2
                           AND timeframe = $3 AND strategy_id = $4
                           AND (
                                order_id <> $5
                                OR filled_at >= NOW() - ($6 || ' seconds')::interval
                           ))""",
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    strategy_id,
                    self.PLACEHOLDER_ORDER_ID,
                    str(int(self.STALE_PLACEHOLDER_TTL_SECONDS)),
                )
                return bool(row)
        except Exception as exc:
            log.warning(
                "db.has_filled_failed",
                key=str(key),
                strategy=strategy_id,
                error=str(exc)[:120],
            )
            return False

    # ── Pessimistic fill-slot claim (audit #322, 2026-04-26) ──────────
    #
    # Design rationale:
    #   The 15s lease (window_claims) is in-flight protection only.
    #   A FAK ladder can take ~2 minutes; the lease auto-expires while
    #   the order is still in flight. Once it expires, the next eval
    #   tick observes has_filled=False (no row written until AFTER the
    #   fill confirms in mark_traded) AND can re-acquire the lease,
    #   then fires a fresh FAK. Smoking gun on window 1777238100:
    #   v10_lgb_only fired 3 FAKs across 21:18:24–21:19:24, then ALL
    #   THREE confirmed with real fills 21:20:37–21:20:56. Single
    #   strategy_window_fills row written (the first), the other two
    #   silently lost the UNIQUE-constraint race after the fact —
    #   trades booked, marker absent.
    #
    # Fix: write a PLACEHOLDER row to strategy_window_fills BEFORE
    # firing FAK. INSERT … ON CONFLICT DO NOTHING returning whether a
    # row was actually inserted. If we lost the race, we don't fire
    # FAK at all. If we won, we own the slot for the rest of the
    # window — no other attempt for the same (window, strategy) can
    # ever win the slot unless we explicitly release it.
    #
    # Release semantics: release_fill_slot ONLY deletes the
    # placeholder (order_id = 'pending'). Once mark_traded UPDATEs the
    # row with a real order_id, release becomes a no-op — protecting
    # us from a late release attempt accidentally clearing a real
    # fill marker.

    PLACEHOLDER_ORDER_ID = "pending"

    # Audit #398 (2026-04-26): defence-in-depth TTL on placeholder rows.
    # If execute_trade's try/finally release fails to fire (engine
    # SIGKILL between try_claim_fill_slot.win and the finally block,
    # node-level kernel panic, etc.), the placeholder would otherwise
    # live forever and lock the strategy out of the window.
    #
    # Smoking-gun precedent: window 1777240500 / v9_lgb_only (22:07:47)
    # left a 'pending' row with no matching trade row, and 12 retries
    # all observed has_filled=True. Manual DELETE was the only recovery.
    #
    # 25s window: tight enough to make stale-pending self-heal feel
    # near-instant, but ≥ FAK ladder hard cap so we never steal an
    # in-flight attempt:
    #   * FAK_LADDER_MAX_ELAPSED_S default = 20s (engine/adapters/
    #     execution/fak_ladder_executor.py). 5s buffer absorbs the
    #     surrounding bookkeeping (DB INSERT, CLOB submit, log flush).
    #   * Healthy try_claim → mark_traded round-trip is <5s including
    #     FAK fill, so a real fill comfortably commits before TTL.
    # Audit 2026-04-27 (PR fix/clob-wallet-and-fak-retry): lowered from
    # 60s → 25s. The old 60s value was sized off the legacy 75s ladder
    # default + safety, but the ladder has been bounded at 20s since
    # 2026-04-26. With execute_trade's failure path already calling
    # release_fill_slot synchronously on success=False, the TTL only
    # matters when the DELETE itself failed (DB pool saturation,
    # SIGKILL between try_claim and finally). Shrinking the safety
    # net from 60s → 25s means a leaked placeholder unblocks the
    # strategy ~35s sooner — which on a 5m window is the difference
    # between "missed entry" and "still in offset band".
    STALE_PLACEHOLDER_TTL_SECONDS = 25

    async def try_claim_fill_slot(
        self,
        key: WindowKey,
        strategy_id: str,
    ) -> bool:
        """Atomically claim the fill slot for (key, strategy_id) BEFORE
        firing the order. Returns True if we won the slot, False if
        another concurrent attempt already holds it.

        Writes a placeholder row to ``strategy_window_fills`` with
        ``order_id = 'pending'``. Subsequent :meth:`has_filled` checks
        return True — including for repeat attempts in the same
        process — preventing a third concurrent attempt from firing.

        Stale-placeholder takeover (audit #398): if a previous attempt
        crashed between try_claim_fill_slot.win and the finally-block
        release (e.g. SIGKILL mid-FAK), the placeholder row would lock
        the strategy out of the window forever. The ON CONFLICT DO
        UPDATE clause STEALS the row when the existing one is still a
        placeholder (order_id = 'pending') AND older than
        :attr:`STALE_PLACEHOLDER_TTL_SECONDS`. A real fill (order_id
        != 'pending') is NEVER overwritten regardless of age.

        On UNIQUE-constraint violation by an active (non-stale)
        placeholder OR a real fill, returns False without raising.
        On any DB error, returns False and logs WARN — fail-closed
        here is correct: better to skip a legitimate trade than book
        a duplicate when our pessimistic guard is unreachable.
        """
        if not self._pool:
            # No DB → no protection possible. Defer to the lease
            # (which is also DB-backed but a 15s window vs a full
            # FAK lifetime is the tighter race we cannot avoid).
            return True
        if not strategy_id:
            return True
        try:
            async with self._pool.acquire() as conn:
                now = datetime.now(timezone.utc)
                # ON CONFLICT DO UPDATE with a WHERE predicate that
                # only matches stale placeholders. The RETURNING shape:
                #   * Fresh INSERT → returns 1.
                #   * Stale-placeholder steal (DO UPDATE matched) → returns 1.
                #   * Active placeholder OR real fill → DO UPDATE no-ops
                #     because WHERE predicate is False; RETURNING returns
                #     no row → fetchval returns None.
                inserted = await conn.fetchval(
                    """
                    INSERT INTO strategy_window_fills
                        (asset, window_ts, timeframe, strategy_id,
                         order_id, filled_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (asset, window_ts, timeframe, strategy_id)
                        DO UPDATE
                            SET order_id = EXCLUDED.order_id,
                                filled_at = EXCLUDED.filled_at
                            WHERE strategy_window_fills.order_id = $5
                              AND strategy_window_fills.filled_at
                                  < NOW() - ($7 || ' seconds')::interval
                    RETURNING 1
                    """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    strategy_id,
                    self.PLACEHOLDER_ORDER_ID,
                    now,
                    str(int(self.STALE_PLACEHOLDER_TTL_SECONDS)),
                )
                won = inserted is not None
                log.info(
                    "db.try_claim_fill_slot",
                    key=str(key),
                    strategy=strategy_id,
                    won=won,
                    ttl_s=self.STALE_PLACEHOLDER_TTL_SECONDS,
                )
                return won
        except Exception as exc:
            log.warning(
                "db.try_claim_fill_slot_failed",
                key=str(key),
                strategy=strategy_id,
                error=str(exc)[:120],
            )
            # Fail-closed: don't proceed with a fill if we cannot
            # establish the pessimistic guard. The caller will return
            # a benign skip reason and try again next eval tick.
            return False

    async def release_fill_slot(
        self,
        key: WindowKey,
        strategy_id: str,
    ) -> None:
        """Release a placeholder claim if FAK didn't fill. Idempotent
        and safe to call on the success path: the WHERE clause
        restricts the DELETE to rows whose order_id is still the
        placeholder — once :meth:`mark_traded` has stamped a real
        order_id, this becomes a no-op.
        """
        if not self._pool:
            return
        if not strategy_id:
            return
        try:
            async with self._pool.acquire() as conn:
                tag = await conn.execute(
                    """
                    DELETE FROM strategy_window_fills
                     WHERE asset = $1
                       AND window_ts = $2
                       AND timeframe = $3
                       AND strategy_id = $4
                       AND order_id = $5
                    """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    strategy_id,
                    self.PLACEHOLDER_ORDER_ID,
                )
                # asyncpg returns 'DELETE <n>' as the command tag.
                rows = 0
                if isinstance(tag, str) and tag.startswith("DELETE "):
                    try:
                        rows = int(tag.split(" ", 1)[1])
                    except (ValueError, IndexError):
                        rows = 0
                # Audit #398 (2026-04-26): promoted to INFO so post-mortem
                # forensics on the fill-slot leak can prove the DELETE
                # actually fired (and hit a row, vs a redundant call that
                # matched 0 rows because mark_traded already stamped a real
                # order_id).
                log.info(
                    "db.release_fill_slot",
                    key=str(key),
                    strategy=strategy_id,
                    rows_deleted=rows,
                )
        except Exception as exc:
            log.warning(
                "db.release_fill_slot_failed",
                key=str(key),
                strategy=strategy_id,
                error=str(exc)[:120],
            )

    # ── Lease-based dedup (audit #316, #317, #320) ──────────────────────
    #
    # Design:
    #   - window_claims (separate table): transient leases with TTL
    #   - window_states (this table): terminal fill records (real order_id)
    #   - PK is (asset, window_ts, timeframe, strategy_id) — each strategy
    #     has its OWN lease per window. Sibling strategies (v9_lgb_only,
    #     v9_ensemble, v10_lgb_only, …) cannot starve each other.
    #   - acquire_lease() returns a claim_id that uniquely identifies the
    #     attempt. Subsequent release/fill calls MUST pass that claim_id —
    #     prevents a slow-failing process from clobbering a successor's lease.
    #   - Expired leases are stolen automatically by the next acquire (no
    #     janitor needed).
    #   - mark_traded is idempotent on the real order_id via window_states PK.
    #
    # Audit #320 forensics (2026-04-26): pre-#320 the PK was per-window
    # only, and a class-level _legacy_claim_ids dict memoised the
    # try_claim_trade claim_id. Sibling strategies firing at the same eval
    # tick clobbered each other's claim_ids in the dict, then
    # clear_trade_claim deleted the WRONG lease (or no-op'd). Hard
    # evidence: window_claims.attempt_n=12, 11 consecutive
    # rows_deleted=0 release logs in 4 minutes, 0 trades in 6 hours.
    # Fix: PK now includes strategy_id, the legacy in-memory shim is
    # dropped, and the API surfaces claim_id explicitly so callers thread
    # it from acquire to release without ever needing a side-table.

    LEASE_TTL_SECONDS = 15

    async def acquire_lease(
        self,
        key: WindowKey,
        *,
        strategy_id: str,
        claimed_by: Optional[str] = None,
    ) -> Optional[str]:
        """Acquire a lease for ``strategy_id`` on ``key``. Returns the
        ``claim_id`` (uuid) on success, or ``None`` if the same strategy
        already holds an active lease for this window.

        Lease semantics (per (asset, window_ts, timeframe, strategy_id)):
          * No row → INSERT, attempt_n=1, return new claim_id.
          * Active lease (expires_at >= now) → return None. The caller's
            previous in-flight attempt for the SAME strategy hasn't
            returned yet; refusing prevents a same-strategy double-fire.
          * Expired lease (expires_at < now) → STEAL: rewrite with new
            claim_id, attempt_n+1, return new claim_id.

        Sibling strategies for the same window NEVER share a row (PK
        includes strategy_id), so they are mutually independent.

        Atomicity: single SQL statement. The ``ON CONFLICT DO UPDATE``
        clause with the ``WHERE`` predicate atomically steals expired
        leases AND rejects active ones — Postgres evaluates the WHERE on
        the existing row before the UPDATE, so an active lease causes
        the row to be untouched and the RETURNING returns nothing.
        """
        if not strategy_id:
            # Defensive: a missing strategy_id would collapse all callers
            # into the same row again, recreating audit #320. Surface
            # immediately rather than silently corrupting the lease table.
            log.error(
                "db.acquire_lease.missing_strategy_id",
                key=str(key),
                hint="strategy_id is required to scope leases per-strategy",
            )
            return None
        if not self._pool:
            # Pool-less mode (tests, paper) — return a synthetic id, caller
            # owns the lease in-process for the call duration.
            return str(uuid.uuid4())
        new_claim_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self.LEASE_TTL_SECONDS)
        claimed_by_str = claimed_by or strategy_id
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO window_claims (
                        asset, window_ts, timeframe, strategy_id,
                        claim_id, claimed_by, claimed_at, expires_at, attempt_n
                    )
                    VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8, 1)
                    ON CONFLICT (asset, window_ts, timeframe, strategy_id) DO UPDATE
                        SET claim_id = EXCLUDED.claim_id,
                            claimed_by = EXCLUDED.claimed_by,
                            claimed_at = EXCLUDED.claimed_at,
                            expires_at = EXCLUDED.expires_at,
                            attempt_n = window_claims.attempt_n + 1
                        WHERE window_claims.expires_at < $7
                    RETURNING claim_id::text, attempt_n
                    """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    strategy_id,
                    new_claim_id,
                    claimed_by_str,
                    now,
                    expires,
                )
                if row is None:
                    log.debug(
                        "db.acquire_lease.busy",
                        key=str(key),
                        strategy=strategy_id,
                    )
                    return None
                claim_id = row["claim_id"]
                attempt_n = row["attempt_n"]
                if attempt_n > 1:
                    log.info(
                        "db.acquire_lease.steal",
                        key=str(key),
                        strategy=strategy_id,
                        attempt_n=attempt_n,
                        claimed_by=claimed_by_str,
                    )
                else:
                    log.debug(
                        "db.acquire_lease",
                        key=str(key),
                        strategy=strategy_id,
                        claim_id=claim_id[:8],
                    )
                return claim_id
        except Exception as exc:
            log.warning(
                "db.acquire_lease_failed",
                key=str(key),
                strategy=strategy_id,
                error=str(exc)[:120],
            )
            return None

    async def release_lease(self, key: WindowKey, claim_id: str) -> None:
        """Release a lease. Only deletes the row if claim_id matches — prevents
        a slow-failing process from accidentally releasing a successor's lease.

        claim_id is globally unique (UUID), so we don't need to scope by
        strategy_id in the WHERE — claim_id alone is sufficient. We still
        scope on (asset, window_ts, timeframe) so the index is used.

        Returns command tag info via INFO log so ops can verify releases land.
        Earlier versions logged at DEBUG which masked silent no-op DELETEs in
        production (audit #317 root-cause: a 0-row DELETE looked identical to a
        successful one).
        """
        if not self._pool:
            return
        if not claim_id:
            log.warning("db.release_lease.no_claim_id", key=str(key))
            return
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    DELETE FROM window_claims
                     WHERE asset = $1 AND window_ts = $2 AND timeframe = $3
                       AND claim_id = $4::uuid
                    """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    claim_id,
                )
            # asyncpg returns a command tag like "DELETE 1" / "DELETE 0".
            try:
                rows_deleted = int(str(result).split()[-1]) if result else 0
            except (ValueError, IndexError):
                rows_deleted = -1
            log.info(
                "db.release_lease",
                key=str(key),
                claim_id=claim_id[:8],
                rows_deleted=rows_deleted,
            )
        except Exception as exc:
            log.warning(
                "db.release_lease_failed", key=str(key), error=str(exc)[:120]
            )

    # ── Public claim API (per-strategy, claim_id explicit) ──────────────
    # Audit #320 (2026-04-26): replaces the old in-memory shim. Callers
    # MUST thread the returned claim_id from try_claim_trade through to
    # clear_trade_claim. No more class-level dict, no more sibling
    # clobbering.

    async def try_claim_trade(
        self,
        key: WindowKey,
        *,
        strategy_id: str,
    ) -> tuple[bool, Optional[str]]:
        """Atomically claim a window for ``strategy_id``.

        Returns ``(True, claim_id)`` on success, ``(False, None)`` when
        the same strategy already holds an active lease for this window.

        Sibling strategies don't conflict — each has its own row keyed
        by (asset, window_ts, timeframe, strategy_id). The first call
        from a given strategy on a given window always succeeds.

        The caller MUST pass the returned ``claim_id`` to
        ``clear_trade_claim`` on any failure path — without this the
        DB row will linger for the full TTL and block subsequent eval
        retries from the same strategy.
        """
        claim_id = await self.acquire_lease(
            key,
            strategy_id=strategy_id,
            claimed_by=strategy_id,
        )
        if claim_id is None:
            return (False, None)
        return (True, claim_id)

    async def clear_trade_claim(
        self,
        key: WindowKey,
        claim_id: Optional[str] = None,
    ) -> None:
        """Release a pending claim using the explicit ``claim_id`` returned
        from ``try_claim_trade``.

        ``claim_id=None`` is a no-op with a WARN log — pre-#320 callers
        relied on an in-memory shim to memoise the claim_id, which
        sibling strategies clobbered. Modern callers MUST pass claim_id
        explicitly. The lease will TTL out naturally within
        LEASE_TTL_SECONDS if the caller forgets, but every dropped
        claim_id is a state-machine bug worth a warning.
        """
        if not claim_id:
            log.warning(
                "db.clear_trade_claim.no_claim_id",
                key=str(key),
                hint=(
                    "clear_trade_claim was called without a claim_id. "
                    "Modern callers must thread the claim_id returned "
                    "by try_claim_trade. Lease will expire naturally "
                    "within LEASE_TTL_SECONDS."
                ),
            )
            return
        await self.release_lease(key, claim_id)

    async def was_resolved(self, key: WindowKey) -> bool:
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT EXISTS(SELECT 1 FROM window_states
                    WHERE asset = $1 AND window_ts = $2 AND timeframe = $3 AND resolved_at IS NOT NULL)""",
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                )
                return bool(row)
        except Exception as exc:
            log.warning("db.was_resolved_failed", key=str(key), error=str(exc)[:120])
            return False

    async def mark_resolved(self, key: WindowKey, outcome: WindowOutcome) -> None:
        if not self._pool:
            return
        outcome_str = str(outcome) if outcome is not None else None
        actual_direction = (
            getattr(outcome, "actual_direction", None) if outcome is not None else None
        )
        try:
            async with self._pool.acquire() as conn:
                # v4.4.0: also persist actual_direction (the oracle-resolved
                # winner) so downstream skip-outcome analysis can read
                # window_states directly instead of reconstructing from
                # trades.market_slug. COALESCE preserves any previously-set
                # value rather than nulling it on subsequent calls.
                # INSERT...ON CONFLICT so outcomes persist even when
                # mark_traded() never ran (GHOST strategies, missed windows).
                # Plain UPDATE silently affected 0 rows → stale outcomes.
                result = await conn.execute(
                    """INSERT INTO window_states
                       (asset, window_ts, timeframe, resolved_at, outcome, actual_direction)
                       VALUES ($1, $4, $5, $2, $3, $6)
                       ON CONFLICT (asset, window_ts, timeframe) DO UPDATE SET
                         resolved_at = EXCLUDED.resolved_at,
                         outcome = EXCLUDED.outcome,
                         actual_direction = COALESCE(EXCLUDED.actual_direction, window_states.actual_direction)
                    """,
                    key.asset,
                    datetime.now(timezone.utc),
                    outcome_str,
                    key.window_ts,
                    key.timeframe,
                    actual_direction,
                )
            log.info(
                "db.mark_resolved",
                key=str(key),
                outcome=outcome_str,
                actual_direction=actual_direction,
            )
        except Exception as exc:
            log.warning("db.mark_resolved_failed", key=str(key), error=str(exc)[:120])

    async def load_recent_traded(self, hours: int) -> set[WindowKey]:
        if not self._pool:
            return set()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT asset, window_ts, timeframe FROM window_states WHERE traded_at > NOW() - ($1 || ' hours')::interval",
                    hours,
                )
                keys: set[WindowKey] = {
                    WindowKey(row["asset"], row["window_ts"], row["timeframe"])
                    for row in rows
                }
                log.info("db.load_recent_traded", count=len(keys), hours=hours)
                return keys
        except Exception as exc:
            log.warning(
                "db.load_recent_traded_failed", hours=hours, error=str(exc)[:120]
            )
            return set()

    async def get_actual_direction(self, key: WindowKey) -> Optional[str]:
        """Return actual_direction from window_snapshots, or None.

        Implements WindowStateRepository.get_actual_direction.
        """
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT actual_direction
                       FROM window_snapshots
                       WHERE window_ts = $1 AND asset = $2
                         AND actual_direction IS NOT NULL
                       LIMIT 1""",
                    key.window_ts,
                    key.asset,
                )
                return row["actual_direction"] if row else None
        except Exception as exc:
            log.warning(
                "pg_window_repo.get_actual_direction_failed",
                error=str(exc)[:100],
            )
            return None

    async def populate_oracle_outcomes(
        self, lookback_seconds: int = 900, min_age_seconds: int = 360
    ) -> int:
        """Poll Polymarket Gamma for resolved 5m BTC UP/DOWN markets and stamp
        oracle_outcome on window_snapshots. Windows must be between
        min_age_seconds and lookback_seconds old.

        Windows resolve every 5 min; this runs on the 2-min reconcile loop so
        every newly-closed window gets oracle_outcome within ~2 min of Gamma
        publishing resolution.
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT DISTINCT window_ts
                       FROM window_snapshots
                       WHERE asset = 'BTC' AND timeframe = '5m'
                         AND oracle_outcome IS NULL
                         AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - $1
                         AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - $2
                       ORDER BY window_ts""",
                    min_age_seconds,
                    lookback_seconds,
                )
        except Exception as exc:
            log.warning("pg_window_repo.poll_oracle_list_failed", error=str(exc)[:100])
            return 0

        windows = [r["window_ts"] for r in rows]
        if not windows:
            return 0

        total_updated = 0
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "novakash-engine/oracle-poll"},
        ) as client:
            sem = asyncio.Semaphore(4)

            async def _fetch(ts: int) -> Optional[str]:
                async with sem:
                    slug = f"{_SLUG_PREFIX}{ts}"
                    try:
                        r = await client.get(
                            f"{_GAMMA_BASE}/events", params={"slug": slug}
                        )
                        if r.status_code != 200:
                            return None
                        data = r.json()
                        if not isinstance(data, list) or not data:
                            return None
                        for event in data:
                            for m in event.get("markets", []) or []:
                                if m.get("slug") != slug:
                                    continue
                                if not m.get("closed"):
                                    return None
                                if m.get("umaResolutionStatus") not in (
                                    None,
                                    "resolved",
                                ):
                                    return None
                                outcomes_raw = m.get("outcomes") or "[]"
                                prices_raw = m.get("outcomePrices") or "[]"
                                try:
                                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                                    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                                except Exception:
                                    return None
                                if not (isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices)):
                                    return None
                                for name, price in zip(outcomes, prices):
                                    try:
                                        if float(price) >= 0.999:
                                            return "UP" if str(name).strip().lower().startswith("u") else "DOWN"
                                    except (TypeError, ValueError):
                                        continue
                        return None
                    except Exception:
                        return None

            results = await asyncio.gather(
                *[_fetch(ts) for ts in windows], return_exceptions=True
            )

        outcomes = [
            (ts, outcome)
            for ts, outcome in zip(windows, results)
            if isinstance(outcome, str) and outcome in ("UP", "DOWN")
        ]
        if not outcomes:
            log.info(
                "pg_window_repo.poll_oracle_no_resolutions",
                polled=len(windows),
            )
            return 0

        # Forward-writer fix follow-up to PR #439 (hub note 297, 2026-05-01):
        # also stamp the canonical ``outcome`` column AND bulk-fill
        # ``signal_evaluations.outcome`` for every row tied to the window. PR
        # #439 only patched the per-trade (order_manager) and shadow-loop
        # paths, but on Montreal those almost never fire — this oracle poll
        # is the only bulk path covering every closed window. Result was that
        # ``window_snapshots.outcome`` stayed NULL on 65% of rows post-deploy.
        signal_eval_total = 0
        try:
            async with self._pool.acquire() as conn:
                for ts, outcome in outcomes:
                    result = await conn.execute(
                        """UPDATE window_snapshots
                           SET oracle_outcome        = $2,
                               poly_resolved_outcome = $2,
                               poly_winner           = $2,
                               outcome               = COALESCE(outcome, $2)
                           WHERE asset = 'BTC' AND timeframe = '5m'
                             AND window_ts = $1
                             AND oracle_outcome IS NULL""",
                        ts,
                        outcome,
                    )
                    total_updated += int(result.split()[-1]) if result else 0

                    # Forward writer: signal_evaluations.outcome — every row
                    # for this window. Idempotent (NULL-only). Inline to avoid
                    # extra pool acquisitions per window.
                    try:
                        se_result = await conn.execute(
                            """UPDATE signal_evaluations
                                  SET outcome = $1
                                WHERE window_ts = $2
                                  AND asset     = 'BTC'
                                  AND timeframe = '5m'
                                  AND outcome IS NULL""",
                            outcome,
                            ts,
                        )
                        signal_eval_total += (
                            int(se_result.split()[-1]) if se_result else 0
                        )
                    except Exception as se_exc:
                        # Surface but keep the loop alive — silent failure
                        # here is what hid the original regression.
                        log.warning(
                            "pg_window_repo.poll_oracle_signal_eval_failed",
                            error=str(se_exc)[:100],
                            window_ts=ts,
                        )
        except Exception as exc:
            log.warning(
                "pg_window_repo.poll_oracle_write_failed", error=str(exc)[:100]
            )
            return total_updated

        log.info(
            "pg_window_repo.poll_oracle_done",
            polled=len(windows),
            resolved=len(outcomes),
            rows_updated=total_updated,
            signal_eval_rows_updated=signal_eval_total,
        )
        return total_updated

    async def label_resolved_windows(self, min_age_seconds: int = 360) -> int:
        """Bulk-stamp actual_direction on resolved windows using oracle sources.

        Implements WindowStateRepository.label_resolved_windows.

        Priority chain (Polymarket 5m markets resolve via Chainlink oracle,
        so Binance tape is the WRONG source — 42.9% disagreement on backfill
        audit 2026-04-18):
            1. window_snapshots.oracle_outcome (Polymarket Gamma, gold)
            2. window_predictions.chainlink_close vs chainlink_open
            3. window_snapshots.delta_chainlink sign (fallback)
            4. NULL (skip — do NOT guess from binance open/close)
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """WITH labels AS (
                           SELECT ws.window_ts, ws.asset, ws.timeframe,
                                  CASE
                                      WHEN ws.oracle_outcome IN ('UP','DOWN') THEN ws.oracle_outcome
                                      WHEN wp.chainlink_close IS NOT NULL
                                           AND wp.chainlink_open IS NOT NULL
                                           AND wp.chainlink_close > wp.chainlink_open THEN 'UP'
                                      WHEN wp.chainlink_close IS NOT NULL
                                           AND wp.chainlink_open IS NOT NULL
                                           AND wp.chainlink_close < wp.chainlink_open THEN 'DOWN'
                                      WHEN ws.delta_chainlink IS NOT NULL
                                           AND ws.delta_chainlink > 0 THEN 'UP'
                                      WHEN ws.delta_chainlink IS NOT NULL
                                           AND ws.delta_chainlink < 0 THEN 'DOWN'
                                      ELSE NULL
                                  END AS label
                           FROM window_snapshots ws
                           LEFT JOIN window_predictions wp
                             ON wp.window_ts = ws.window_ts
                            AND wp.asset     = ws.asset
                            AND wp.timeframe = ws.timeframe
                           WHERE ws.actual_direction IS NULL
                             AND ws.window_ts < EXTRACT(EPOCH FROM NOW())::bigint - $1
                       )
                       UPDATE window_snapshots ws
                       SET actual_direction = labels.label
                       FROM labels
                       WHERE ws.window_ts = labels.window_ts
                         AND ws.asset     = labels.asset
                         AND ws.timeframe = labels.timeframe
                         AND labels.label IS NOT NULL""",
                    min_age_seconds,
                )
                count = int(result.split()[-1]) if result else 0
                if count > 0:
                    log.info(
                        "pg_window_repo.labeled_windows",
                        count=count,
                    )
                return count
        except Exception as exc:
            log.warning(
                "pg_window_repo.label_resolved_windows_failed",
                error=str(exc)[:100],
            )
            return 0
