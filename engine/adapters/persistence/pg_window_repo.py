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

from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog

from domain.ports import WindowStateRepository
from domain.value_objects import WindowKey, WindowOutcome

log = structlog.get_logger(__name__)


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

        Verbatim SQL from ``DBClient.update_window_outcome``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET outcome = $1, pnl_usd = $2, poly_winner = $3
                    WHERE window_ts = $4 AND asset = $5 AND timeframe = $6
                    """,
                    outcome,
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
                outcome=outcome,
                pnl_usd=pnl_usd,
            )
        except Exception as exc:
            log.error("db.update_window_outcome_failed", error=str(exc))

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

        Verbatim SQL from ``DBClient.update_shadow_resolution``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET oracle_outcome  = $1,
                        shadow_pnl      = $2,
                        shadow_would_win = $3
                    WHERE window_ts = $4 AND asset = $5 AND timeframe = $6
                    """,
                    oracle_outcome,
                    shadow_pnl,
                    shadow_would_win,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.shadow_resolution_updated",
                window_ts=window_ts,
                asset=asset,
                oracle_outcome=oracle_outcome,
                shadow_pnl=f"{shadow_pnl:+.2f}",
                shadow_would_win=shadow_would_win,
            )
        except Exception as exc:
            log.error("db.update_shadow_resolution_failed", error=str(exc))

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

    async def mark_traded(self, key: WindowKey, order_id: str) -> None:
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
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
                    datetime.now(timezone.utc),
                    order_id,
                )
            log.debug(
                "db.mark_traded",
                key=str(key),
                order_id=order_id[:20] if order_id else None,
            )
        except Exception as exc:
            log.warning("db.mark_traded_failed", key=str(key), error=str(exc)[:120])

    async def try_claim_trade(self, key: WindowKey) -> bool:
        if not self._pool:
            return True
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    INSERT INTO window_states (asset, window_ts, timeframe, traded_at, order_id)
                    VALUES ($1, $2, $3, $4, 'pending')
                    ON CONFLICT (asset, window_ts, timeframe) DO NOTHING
                    RETURNING 1
                    """,
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                    datetime.now(timezone.utc),
                )
                claimed = bool(row)
            log.debug("db.try_claim_trade", key=str(key), claimed=claimed)
            return claimed
        except Exception as exc:
            log.warning("db.try_claim_trade_failed", key=str(key), error=str(exc)[:120])
            return False

    async def clear_trade_claim(self, key: WindowKey) -> None:
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM window_states WHERE asset = $1 AND window_ts = $2 AND timeframe = $3 AND order_id = 'pending'",
                    key.asset,
                    key.window_ts,
                    key.timeframe,
                )
            log.debug("db.clear_trade_claim", key=str(key))
        except Exception as exc:
            log.warning(
                "db.clear_trade_claim_failed", key=str(key), error=str(exc)[:120]
            )

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
                await conn.execute(
                    "UPDATE window_states SET resolved_at = $2, outcome = $3, "
                    "actual_direction = COALESCE($6, actual_direction) "
                    "WHERE asset = $1 AND window_ts = $4 AND timeframe = $5",
                    key.asset,
                    datetime.now(timezone.utc),
                    outcome_str,
                    key.window_ts,
                    key.timeframe,
                    actual_direction,
                )
            log.debug(
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

    async def label_resolved_windows(self, min_age_seconds: int = 360) -> int:
        """Bulk-stamp actual_direction on windows with close_price but no label.

        Implements WindowStateRepository.label_resolved_windows.
        Polymarket 5-min UP/DOWN resolves via Chainlink oracle:
        close_price > open_price → UP, else DOWN.
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """UPDATE window_snapshots
                       SET actual_direction = CASE
                           WHEN close_price > open_price THEN 'UP'
                           ELSE 'DOWN'
                       END
                       WHERE actual_direction IS NULL
                         AND close_price IS NOT NULL
                         AND open_price IS NOT NULL
                         AND close_price != open_price
                         AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - $1""",
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
