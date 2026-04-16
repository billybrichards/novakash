"""PostgreSQL Signal Repository -- per-aggregate persistence for signal data.

Implements :class:`engine.domain.ports.SignalRepository` by delegating to
the **exact same SQL** that ``engine/persistence/db_client.py`` uses today.
This is a thin structural split -- zero behaviour change.

Phase 2 will wire this into the composition root.  Until then, nothing
imports this module so there is zero runtime risk.

Audit: CA-01 (Clean Architecture migration -- split god-class DBClient).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import asyncpg
import structlog

from domain.ports import SignalRepository

log = structlog.get_logger(__name__)


class PgSignalRepository(SignalRepository):
    """asyncpg-backed signal repository.

    Accepts an ``asyncpg.Pool`` -- the same pool the legacy ``DBClient``
    uses.  Methods copy SQL verbatim from ``db_client.py`` so behaviour
    parity is byte-for-byte.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- SignalRepository port methods -------------------------------------

    async def write_signal_evaluation(self, data: dict) -> None:  # type: ignore[override]
        """Persist one signal evaluation row to ``signal_evaluations`` table.

        Verbatim SQL from ``DBClient.write_signal_evaluation``.
        """
        # TODO: TECH_DEBT - accept SignalEvaluation VO once Phase 1 populates its fields
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signal_evaluations (
                        window_ts, asset, timeframe, eval_offset,
                        clob_up_bid, clob_up_ask, clob_down_bid, clob_down_ask,
                        binance_price, tiingo_open, tiingo_close, chainlink_price,
                        delta_pct, delta_tiingo, delta_binance, delta_chainlink, delta_source,
                        vpin, regime, clob_spread, clob_mid,
                        v2_probability_up, v2_direction, v2_agrees, v2_high_conf,
                        v2_model_version, v2_quantiles, v2_quantiles_at_close,
                        gate_vpin_passed, gate_delta_passed, gate_cg_passed,
                        gate_twap_passed, gate_timesfm_passed, gate_passed,
                        gate_failed, decision,
                        twap_delta, twap_direction, twap_gamma_agree
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11, $12,
                        $13, $14, $15, $16, $17,
                        $18, $19, $20, $21,
                        $22, $23, $24, $25,
                        $26, $27, $28,
                        $29, $30, $31,
                        $32, $33, $34, $35,
                        $36, $37, $38, $39
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        clob_up_bid           = EXCLUDED.clob_up_bid,
                        clob_up_ask           = EXCLUDED.clob_up_ask,
                        clob_down_bid         = EXCLUDED.clob_down_bid,
                        clob_down_ask         = EXCLUDED.clob_down_ask,
                        binance_price         = EXCLUDED.binance_price,
                        tiingo_open           = EXCLUDED.tiingo_open,
                        tiingo_close          = EXCLUDED.tiingo_close,
                        chainlink_price       = EXCLUDED.chainlink_price,
                        delta_pct             = EXCLUDED.delta_pct,
                        delta_tiingo          = EXCLUDED.delta_tiingo,
                        delta_binance         = EXCLUDED.delta_binance,
                        delta_chainlink       = EXCLUDED.delta_chainlink,
                        delta_source          = EXCLUDED.delta_source,
                        vpin                  = EXCLUDED.vpin,
                        regime                = EXCLUDED.regime,
                        clob_spread           = EXCLUDED.clob_spread,
                        clob_mid              = EXCLUDED.clob_mid,
                        v2_probability_up     = EXCLUDED.v2_probability_up,
                        v2_direction          = EXCLUDED.v2_direction,
                        v2_agrees             = EXCLUDED.v2_agrees,
                        v2_high_conf          = EXCLUDED.v2_high_conf,
                        v2_model_version      = EXCLUDED.v2_model_version,
                        v2_quantiles          = EXCLUDED.v2_quantiles,
                        v2_quantiles_at_close = EXCLUDED.v2_quantiles_at_close,
                        gate_vpin_passed      = EXCLUDED.gate_vpin_passed,
                        gate_delta_passed     = EXCLUDED.gate_delta_passed,
                        gate_cg_passed        = EXCLUDED.gate_cg_passed,
                        gate_twap_passed      = EXCLUDED.gate_twap_passed,
                        gate_timesfm_passed   = EXCLUDED.gate_timesfm_passed,
                        gate_passed           = EXCLUDED.gate_passed,
                        gate_failed           = EXCLUDED.gate_failed,
                        decision              = EXCLUDED.decision,
                        twap_delta            = EXCLUDED.twap_delta,
                        twap_direction        = EXCLUDED.twap_direction,
                        twap_gamma_agree      = EXCLUDED.twap_gamma_agree,
                        evaluated_at          = NOW()
                    """,
                    int(data.get("window_ts", 0)),
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    data.get("eval_offset"),
                    float(data["clob_up_bid"]) if data.get("clob_up_bid") is not None else None,
                    float(data["clob_up_ask"]) if data.get("clob_up_ask") is not None else None,
                    float(data["clob_down_bid"]) if data.get("clob_down_bid") is not None else None,
                    float(data["clob_down_ask"]) if data.get("clob_down_ask") is not None else None,
                    float(data["binance_price"]) if data.get("binance_price") is not None else None,
                    float(data["tiingo_open"]) if data.get("tiingo_open") is not None else None,
                    float(data["tiingo_close"]) if data.get("tiingo_close") is not None else None,
                    float(data["chainlink_price"]) if data.get("chainlink_price") is not None else None,
                    float(data["delta_pct"]) if data.get("delta_pct") is not None else None,
                    float(data["delta_tiingo"]) if data.get("delta_tiingo") is not None else None,
                    float(data["delta_binance"]) if data.get("delta_binance") is not None else None,
                    float(data["delta_chainlink"]) if data.get("delta_chainlink") is not None else None,
                    data.get("delta_source"),
                    float(data["vpin"]) if data.get("vpin") is not None else None,
                    data.get("regime"),
                    float(data["clob_spread"]) if data.get("clob_spread") is not None else None,
                    float(data["clob_mid"]) if data.get("clob_mid") is not None else None,
                    float(data["v2_probability_up"]) if data.get("v2_probability_up") is not None else None,
                    data.get("v2_direction"),
                    bool(data["v2_agrees"]) if data.get("v2_agrees") is not None else None,
                    bool(data["v2_high_conf"]) if data.get("v2_high_conf") is not None else None,
                    data.get("v2_model_version"),
                    data.get("v2_quantiles"),  # JSONB (already serialized as JSON string)
                    data.get("v2_quantiles_at_close"),  # JSONB
                    bool(data["gate_vpin_passed"]) if data.get("gate_vpin_passed") is not None else None,
                    bool(data["gate_delta_passed"]) if data.get("gate_delta_passed") is not None else None,
                    bool(data["gate_cg_passed"]) if data.get("gate_cg_passed") is not None else None,
                    bool(data["gate_twap_passed"]) if data.get("gate_twap_passed") is not None else None,
                    bool(data["gate_timesfm_passed"]) if data.get("gate_timesfm_passed") is not None else None,
                    bool(data.get("gate_passed", False)),
                    data.get("gate_failed"),
                    data.get("decision", "SKIP"),
                    float(data["twap_delta"]) if data.get("twap_delta") is not None else None,
                    data.get("twap_direction"),
                    bool(data["twap_gamma_agree"]) if data.get("twap_gamma_agree") is not None else None
                )
        except Exception as exc:
            log.warning("db.write_signal_evaluation_failed", error=str(exc)[:200])

    async def write_clob_snapshot(self, data: dict) -> None:  # type: ignore[override]
        """Persist one CLOB book snapshot to ``clob_book_snapshots`` table.

        Verbatim SQL from ``DBClient.write_clob_book_snapshot``.
        """
        # TODO: TECH_DEBT - accept ClobSnapshot VO once Phase 1 populates its fields
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO clob_book_snapshots (
                        asset, timeframe, window_ts,
                        up_token_id, down_token_id,
                        up_best_bid, up_best_ask, up_bid_depth, up_ask_depth,
                        down_best_bid, down_best_ask, down_bid_depth, down_ask_depth,
                        up_spread, down_spread, mid_price,
                        up_bids_top5, up_asks_top5, down_bids_top5, down_asks_top5
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15, $16, $17, $18, $19, $20
                    )
                    ON CONFLICT (window_ts, up_token_id, down_token_id, ts) DO NOTHING
                    """,
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    int(data.get("window_ts", 0)),
                    data.get("up_token_id"),
                    data.get("down_token_id"),
                    float(data["up_best_bid"]) if data.get("up_best_bid") is not None else None,
                    float(data["up_best_ask"]) if data.get("up_best_ask") is not None else None,
                    float(data["up_bid_depth"]) if data.get("up_bid_depth") is not None else None,
                    float(data["up_ask_depth"]) if data.get("up_ask_depth") is not None else None,
                    float(data["down_best_bid"]) if data.get("down_best_bid") is not None else None,
                    float(data["down_best_ask"]) if data.get("down_best_ask") is not None else None,
                    float(data["down_bid_depth"]) if data.get("down_bid_depth") is not None else None,
                    float(data["down_ask_depth"]) if data.get("down_ask_depth") is not None else None,
                    float(data["up_spread"]) if data.get("up_spread") is not None else None,
                    float(data["down_spread"]) if data.get("down_spread") is not None else None,
                    float(data["mid_price"]) if data.get("mid_price") is not None else None,
                    data.get("up_bids_top5", []),
                    data.get("up_asks_top5", []),
                    data.get("down_bids_top5", []),
                    data.get("down_asks_top5", [])
                )
        except Exception as exc:
            log.warning("db.write_clob_book_snapshot_failed", error=str(exc)[:200])

    async def write_gate_audit(self, data: dict) -> None:  # type: ignore[override]
        """Retired — gate_audit superseded by gate_check_traces (feat/trace PR).

        Intentionally a no-op.  Gate-check persistence now goes through
        WindowTraceRepository which writes to ``gate_check_traces``.
        """
        return  # no-op

    async def write_window_snapshot(self, snapshot: dict) -> None:  # type: ignore[override]
        """Persist a 5m/15m window evaluation snapshot.

        Verbatim SQL from ``DBClient.write_window_snapshot``.
        All fields are optional -- missing keys default to None.
        Conflicts on (window_ts, asset, timeframe, eval_offset) are upserted.
        """
        # TODO: TECH_DEBT - accept WindowSnapshot VO once Phase 1 populates its fields
        if not self._pool:
            return
        try:
            # Normalise confidence to float if it's a string
            confidence = snapshot.get("confidence")
            if isinstance(confidence, str):
                _conf_map = {"HIGH": 0.85, "MODERATE": 0.65, "LOW": 0.45, "NONE": 0.20}
                confidence = _conf_map.get(confidence.upper(), 0.5)

            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_snapshots (
                        window_ts, asset, timeframe,
                        open_price, close_price, delta_pct, vpin, regime,
                        cg_connected, cg_oi_usd, cg_oi_delta_pct,
                        cg_liq_long_usd, cg_liq_short_usd, cg_liq_total_usd,
                        cg_long_pct, cg_short_pct, cg_long_short_ratio,
                        cg_top_long_pct, cg_top_short_pct, cg_top_ratio,
                        cg_taker_buy_usd, cg_taker_sell_usd, cg_funding_rate,
                        direction, confidence, cg_modifier,
                        trade_placed, skip_reason,
                        outcome, pnl_usd, poly_winner, btc_price,
                        twap_delta_pct, twap_direction, twap_gamma_agree,
                        twap_agreement_score, twap_confidence_boost,
                        twap_n_ticks, twap_stability,
                        twap_trend_pct, twap_momentum_pct, twap_gamma_gate,
                        twap_should_skip, twap_skip_reason,
                        timesfm_direction, timesfm_confidence,
                        timesfm_predicted_close, timesfm_delta_vs_open,
                        timesfm_spread, timesfm_p10, timesfm_p50, timesfm_p90,
                        market_best_bid, market_best_ask,
                        market_spread, market_mid_price,
                        market_volume, market_liquidity,
                        v71_would_trade, v71_skip_reason, v71_regime,
                        is_live,
                        gamma_up_price, gamma_down_price,
                        delta_chainlink, delta_tiingo, delta_binance, price_consensus,
                        engine_version, delta_source, confidence_tier,
                        gates_passed, gate_failed,
                        shadow_trade_direction, shadow_trade_entry_price,
                        v2_probability_up, v2_direction, v2_agrees,
                        v2_model_version, eval_offset,
                        v2_quantiles, v2_quantiles_at_close,
                        sub_signal_elm, sub_signal_cascade, sub_signal_taker,
                        sub_signal_vpin, sub_signal_momentum,
                        sub_signal_oi, sub_signal_funding,
                        regime_confidence, regime_persistence,
                        strategy_conviction, strategy_conviction_score,
                        consensus_safe_to_trade, consensus_agreement_score,
                        consensus_divergence_bps,
                        macro_bias, macro_direction_gate, macro_size_modifier
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,
                        $9,$10,$11,$12,$13,$14,$15,$16,$17,
                        $18,$19,$20,$21,$22,$23,
                        $24,$25,$26,$27,$28,$29,$30,$31,$32,
                        $33,$34,$35,$36,$37,$38,$39,
                        $40,$41,$42,$43,$44,
                        $45,$46,$47,$48,$49,$50,$51,$52,
                        $53,$54,$55,$56,$57,$58,
                        $59,$60,$61,
                        $62,
                        $63,$64,
                        $65,$66,$67,$68,
                        $69,$70,$71,
                        $72,$73,
                        $74,$75,$76,$77,$78,
                        $79,$80,$81,$82,
                        $83,$84,$85,$86,$87,
                        $88,$89,
                        $90,$91,
                        $92,$93,
                        $94,$95,
                        $96,
                        $97,$98,$99
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        gamma_up_price         = COALESCE(EXCLUDED.gamma_up_price, window_snapshots.gamma_up_price),
                        gamma_down_price       = COALESCE(EXCLUDED.gamma_down_price, window_snapshots.gamma_down_price),
                        delta_chainlink        = COALESCE(EXCLUDED.delta_chainlink, window_snapshots.delta_chainlink),
                        delta_tiingo           = COALESCE(EXCLUDED.delta_tiingo, window_snapshots.delta_tiingo),
                        delta_binance          = COALESCE(EXCLUDED.delta_binance, window_snapshots.delta_binance),
                        price_consensus        = COALESCE(EXCLUDED.price_consensus, window_snapshots.price_consensus),
                        engine_version         = COALESCE(EXCLUDED.engine_version, window_snapshots.engine_version),
                        delta_source           = COALESCE(EXCLUDED.delta_source, window_snapshots.delta_source),
                        confidence_tier        = COALESCE(EXCLUDED.confidence_tier, window_snapshots.confidence_tier),
                        gates_passed           = COALESCE(EXCLUDED.gates_passed, window_snapshots.gates_passed),
                        gate_failed            = COALESCE(EXCLUDED.gate_failed, window_snapshots.gate_failed),
                        shadow_trade_direction = COALESCE(EXCLUDED.shadow_trade_direction, window_snapshots.shadow_trade_direction),
                        shadow_trade_entry_price = COALESCE(EXCLUDED.shadow_trade_entry_price, window_snapshots.shadow_trade_entry_price),
                        v2_probability_up      = COALESCE(EXCLUDED.v2_probability_up, window_snapshots.v2_probability_up),
                        v2_direction           = COALESCE(EXCLUDED.v2_direction, window_snapshots.v2_direction),
                        v2_agrees              = COALESCE(EXCLUDED.v2_agrees, window_snapshots.v2_agrees),
                        v2_model_version       = COALESCE(EXCLUDED.v2_model_version, window_snapshots.v2_model_version),
                        eval_offset            = COALESCE(EXCLUDED.eval_offset, window_snapshots.eval_offset),
                        v2_quantiles           = COALESCE(EXCLUDED.v2_quantiles, window_snapshots.v2_quantiles),
                        v2_quantiles_at_close  = COALESCE(EXCLUDED.v2_quantiles_at_close, window_snapshots.v2_quantiles_at_close),
                        sub_signal_elm         = COALESCE(EXCLUDED.sub_signal_elm, window_snapshots.sub_signal_elm),
                        sub_signal_cascade     = COALESCE(EXCLUDED.sub_signal_cascade, window_snapshots.sub_signal_cascade),
                        sub_signal_taker       = COALESCE(EXCLUDED.sub_signal_taker, window_snapshots.sub_signal_taker),
                        sub_signal_vpin        = COALESCE(EXCLUDED.sub_signal_vpin, window_snapshots.sub_signal_vpin),
                        sub_signal_momentum    = COALESCE(EXCLUDED.sub_signal_momentum, window_snapshots.sub_signal_momentum),
                        sub_signal_oi          = COALESCE(EXCLUDED.sub_signal_oi, window_snapshots.sub_signal_oi),
                        sub_signal_funding     = COALESCE(EXCLUDED.sub_signal_funding, window_snapshots.sub_signal_funding),
                        regime_confidence      = COALESCE(EXCLUDED.regime_confidence, window_snapshots.regime_confidence),
                        regime_persistence     = COALESCE(EXCLUDED.regime_persistence, window_snapshots.regime_persistence),
                        strategy_conviction    = COALESCE(EXCLUDED.strategy_conviction, window_snapshots.strategy_conviction),
                        strategy_conviction_score = COALESCE(EXCLUDED.strategy_conviction_score, window_snapshots.strategy_conviction_score),
                        consensus_safe_to_trade = COALESCE(EXCLUDED.consensus_safe_to_trade, window_snapshots.consensus_safe_to_trade),
                        consensus_agreement_score = COALESCE(EXCLUDED.consensus_agreement_score, window_snapshots.consensus_agreement_score),
                        consensus_divergence_bps = COALESCE(EXCLUDED.consensus_divergence_bps, window_snapshots.consensus_divergence_bps),
                        macro_bias             = COALESCE(EXCLUDED.macro_bias, window_snapshots.macro_bias),
                        macro_direction_gate   = COALESCE(EXCLUDED.macro_direction_gate, window_snapshots.macro_direction_gate),
                        macro_size_modifier    = COALESCE(EXCLUDED.macro_size_modifier, window_snapshots.macro_size_modifier)
                    """,
                    snapshot.get("window_ts"),
                    snapshot.get("asset", "BTC"),
                    snapshot.get("timeframe", "5m"),
                    snapshot.get("open_price"),
                    snapshot.get("close_price"),
                    snapshot.get("delta_pct"),
                    snapshot.get("vpin"),
                    snapshot.get("regime"),
                    snapshot.get("cg_connected", False),
                    snapshot.get("cg_oi_usd"),
                    snapshot.get("cg_oi_delta_pct"),
                    snapshot.get("cg_liq_long_usd"),
                    snapshot.get("cg_liq_short_usd"),
                    snapshot.get("cg_liq_total_usd"),
                    snapshot.get("cg_long_pct"),
                    snapshot.get("cg_short_pct"),
                    snapshot.get("cg_long_short_ratio"),
                    snapshot.get("cg_top_long_pct"),
                    snapshot.get("cg_top_short_pct"),
                    snapshot.get("cg_top_ratio"),
                    snapshot.get("cg_taker_buy_usd"),
                    snapshot.get("cg_taker_sell_usd"),
                    snapshot.get("cg_funding_rate"),
                    snapshot.get("direction"),
                    confidence,
                    snapshot.get("cg_modifier"),
                    snapshot.get("trade_placed", False),
                    snapshot.get("skip_reason"),
                    snapshot.get("outcome"),
                    snapshot.get("pnl_usd"),
                    snapshot.get("poly_winner"),
                    snapshot.get("btc_price"),
                    snapshot.get("twap_delta_pct"),
                    snapshot.get("twap_direction"),
                    snapshot.get("twap_gamma_agree"),
                    snapshot.get("twap_agreement_score"),
                    snapshot.get("twap_confidence_boost"),
                    snapshot.get("twap_n_ticks"),
                    snapshot.get("twap_stability"),
                    # v5.7c: trend + momentum + gamma gate
                    snapshot.get("twap_trend_pct"),
                    snapshot.get("twap_momentum_pct"),
                    snapshot.get("twap_gamma_gate"),
                    snapshot.get("twap_should_skip"),
                    snapshot.get("twap_skip_reason"),
                    # v6.0: TimesFM forecast
                    snapshot.get("timesfm_direction"),
                    snapshot.get("timesfm_confidence"),
                    snapshot.get("timesfm_predicted_close"),
                    snapshot.get("timesfm_delta_vs_open"),
                    snapshot.get("timesfm_spread"),
                    snapshot.get("timesfm_p10"),
                    snapshot.get("timesfm_p50"),
                    snapshot.get("timesfm_p90"),
                    # v6.0: Spread/liquidity
                    snapshot.get("market_best_bid"),
                    snapshot.get("market_best_ask"),
                    snapshot.get("market_spread"),
                    snapshot.get("market_mid_price"),
                    snapshot.get("market_volume"),
                    snapshot.get("market_liquidity"),
                    snapshot.get("v71_would_trade"),
                    snapshot.get("v71_skip_reason"),
                    snapshot.get("v71_regime"),
                    snapshot.get("is_live", False),
                    # gamma prices (fetched at T-60 and included in snapshot dict)
                    snapshot.get("gamma_up_price"),
                    snapshot.get("gamma_down_price"),
                    # v7.2: multi-source deltas
                    snapshot.get("delta_chainlink"),
                    snapshot.get("delta_tiingo"),
                    snapshot.get("delta_binance"),
                    snapshot.get("price_consensus"),
                    # v8.0: engine metadata + gate audit + shadow trade
                    snapshot.get("engine_version", "v8.0"),
                    snapshot.get("delta_source"),
                    snapshot.get("confidence_tier"),
                    snapshot.get("gates_passed"),
                    snapshot.get("gate_failed"),
                    snapshot.get("shadow_trade_direction"),
                    snapshot.get("shadow_trade_entry_price"),
                    # v8.1: OAK (v2.2) early entry gate
                    snapshot.get("v2_probability_up"),
                    snapshot.get("v2_direction"),
                    snapshot.get("v2_agrees"),
                    snapshot.get("v2_model_version"),
                    snapshot.get("eval_offset"),
                    snapshot.get("v2_quantiles"),
                    snapshot.get("v2_quantiles_at_close"),
                    # v4.4.0 denormalised v3/v4 surface
                    snapshot.get("sub_signal_elm"),
                    snapshot.get("sub_signal_cascade"),
                    snapshot.get("sub_signal_taker"),
                    snapshot.get("sub_signal_vpin"),
                    snapshot.get("sub_signal_momentum"),
                    snapshot.get("sub_signal_oi"),
                    snapshot.get("sub_signal_funding"),
                    snapshot.get("regime_confidence"),
                    snapshot.get("regime_persistence"),
                    snapshot.get("strategy_conviction"),
                    snapshot.get("strategy_conviction_score"),
                    snapshot.get("consensus_safe_to_trade"),
                    snapshot.get("consensus_agreement_score"),
                    snapshot.get("consensus_divergence_bps"),
                    snapshot.get("macro_bias"),
                    snapshot.get("macro_direction_gate"),
                    snapshot.get("macro_size_modifier"),
                )
            log.debug(
                "db.window_snapshot_written",
                asset=snapshot.get("asset"),
                timeframe=snapshot.get("timeframe"),
                window_ts=snapshot.get("window_ts"),
            )
        except Exception as exc:
            log.error(
                "db.write_window_snapshot_failed",
                error=str(exc),
                asset=snapshot.get("asset"),
                window_ts=snapshot.get("window_ts"),
            )
            # Never re-raise -- DB writes must not crash the engine

    async def update_window_surface_fields(
        self,
        *,
        window_ts: int,
        asset: str,
        timeframe: str,
        eval_offset: Optional[int],
        surface_fields: dict,
    ) -> None:
        """Upsert the v3/v4 surface columns on an existing window_snapshots row.

        v4.4.0 (2026-04-16): the legacy writer in five_min_vpin.py doesn't
        have a FullDataSurface handle, but the strategy registry does —
        this method is the registry's writer path for the 17 v3/v4
        columns so analysts can query them as first-class columns without
        extracting from window_evaluation_traces.surface_json.

        Fire-and-forget: never raises. Uses an INSERT ... ON CONFLICT upsert
        so it creates a minimal row if none exists yet (possible on ghost
        strategies whose evaluate runs before the legacy write).
        """
        if not self._pool:
            return
        if eval_offset is None:
            eval_offset = 0
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_snapshots (
                        window_ts, asset, timeframe, eval_offset,
                        sub_signal_elm, sub_signal_cascade, sub_signal_taker,
                        sub_signal_vpin, sub_signal_momentum,
                        sub_signal_oi, sub_signal_funding,
                        regime_confidence, regime_persistence,
                        strategy_conviction, strategy_conviction_score,
                        consensus_safe_to_trade, consensus_agreement_score,
                        consensus_divergence_bps,
                        macro_bias, macro_direction_gate, macro_size_modifier
                    ) VALUES (
                        $1,$2,$3,$4,
                        $5,$6,$7,$8,$9,$10,$11,
                        $12,$13,$14,$15,
                        $16,$17,$18,
                        $19,$20,$21
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        sub_signal_elm         = COALESCE(EXCLUDED.sub_signal_elm, window_snapshots.sub_signal_elm),
                        sub_signal_cascade     = COALESCE(EXCLUDED.sub_signal_cascade, window_snapshots.sub_signal_cascade),
                        sub_signal_taker       = COALESCE(EXCLUDED.sub_signal_taker, window_snapshots.sub_signal_taker),
                        sub_signal_vpin        = COALESCE(EXCLUDED.sub_signal_vpin, window_snapshots.sub_signal_vpin),
                        sub_signal_momentum    = COALESCE(EXCLUDED.sub_signal_momentum, window_snapshots.sub_signal_momentum),
                        sub_signal_oi          = COALESCE(EXCLUDED.sub_signal_oi, window_snapshots.sub_signal_oi),
                        sub_signal_funding     = COALESCE(EXCLUDED.sub_signal_funding, window_snapshots.sub_signal_funding),
                        regime_confidence      = COALESCE(EXCLUDED.regime_confidence, window_snapshots.regime_confidence),
                        regime_persistence     = COALESCE(EXCLUDED.regime_persistence, window_snapshots.regime_persistence),
                        strategy_conviction    = COALESCE(EXCLUDED.strategy_conviction, window_snapshots.strategy_conviction),
                        strategy_conviction_score = COALESCE(EXCLUDED.strategy_conviction_score, window_snapshots.strategy_conviction_score),
                        consensus_safe_to_trade = COALESCE(EXCLUDED.consensus_safe_to_trade, window_snapshots.consensus_safe_to_trade),
                        consensus_agreement_score = COALESCE(EXCLUDED.consensus_agreement_score, window_snapshots.consensus_agreement_score),
                        consensus_divergence_bps = COALESCE(EXCLUDED.consensus_divergence_bps, window_snapshots.consensus_divergence_bps),
                        macro_bias             = COALESCE(EXCLUDED.macro_bias, window_snapshots.macro_bias),
                        macro_direction_gate   = COALESCE(EXCLUDED.macro_direction_gate, window_snapshots.macro_direction_gate),
                        macro_size_modifier    = COALESCE(EXCLUDED.macro_size_modifier, window_snapshots.macro_size_modifier)
                    """,
                    int(window_ts),
                    asset,
                    timeframe,
                    int(eval_offset),
                    surface_fields.get("sub_signal_elm"),
                    surface_fields.get("sub_signal_cascade"),
                    surface_fields.get("sub_signal_taker"),
                    surface_fields.get("sub_signal_vpin"),
                    surface_fields.get("sub_signal_momentum"),
                    surface_fields.get("sub_signal_oi"),
                    surface_fields.get("sub_signal_funding"),
                    surface_fields.get("regime_confidence"),
                    surface_fields.get("regime_persistence"),
                    surface_fields.get("strategy_conviction"),
                    surface_fields.get("strategy_conviction_score"),
                    surface_fields.get("consensus_safe_to_trade"),
                    surface_fields.get("consensus_agreement_score"),
                    surface_fields.get("consensus_divergence_bps"),
                    surface_fields.get("macro_bias"),
                    surface_fields.get("macro_direction_gate"),
                    surface_fields.get("macro_size_modifier"),
                )
        except Exception as exc:
            log.warning(
                "pg_signal_repo.update_window_surface_fields_failed",
                error=str(exc)[:160],
                asset=asset,
                window_ts=window_ts,
            )

    # -- Additional signal-related methods (not on port yet) ---------------
    # These are included here because they belong to the signal aggregate
    # even though the port interface uses placeholder VOs today.  Phase 1
    # will add typed VO signatures; until then the dict-based API is kept
    # for 1:1 parity with DBClient.

    async def write_signal(
        self,
        signal_type: str,
        value: float,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Persist a signal snapshot to the ``signals`` table.

        Verbatim SQL from ``DBClient.write_signal``.
        """
        if not self._pool:
            return

        ts = timestamp or datetime.utcnow()
        query = """
            INSERT INTO signals (signal_type, value, metadata, created_at)
            VALUES ($1, $2, $3::jsonb, $4)
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    query,
                    signal_type,
                    float(value),
                    json.dumps(metadata or {}),
                    ts,
                )
            log.debug("db.signal_written", type=signal_type, value=value)
        except Exception as exc:
            log.error("db.write_signal_failed", type=signal_type, error=str(exc))
            raise

    async def write_clob_execution_log(self, data: dict) -> None:
        """Log comprehensive CLOB execution data for every FOK attempt.

        Verbatim SQL from ``DBClient.write_clob_execution_log``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO clob_execution_log (
                        asset, timeframe, window_ts, outcome, token_id,
                        direction, strategy, eval_offset,
                        target_price, target_size, max_price, min_price,
                        clob_best_ask, clob_best_bid,
                        execution_mode, fok_attempt_num, fok_max_attempts,
                        status, fill_price, fill_size, order_id,
                        error_code, error_message, latency_ms, metadata
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, $19, $20, $21,
                        $22, $23, $24, $25
                    )
                    ON CONFLICT (window_ts, outcome, ts, execution_mode, fok_attempt_num)
                    DO NOTHING
                    """,
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    int(data.get("window_ts", 0)),
                    data.get("outcome", "UP"),
                    data.get("token_id"),
                    data.get("direction", "BUY"),
                    data.get("strategy"),
                    data.get("eval_offset"),
                    float(data["target_price"]) if data.get("target_price") is not None else None,
                    float(data["target_size"]) if data.get("target_size") is not None else None,
                    float(data["max_price"]) if data.get("max_price") is not None else None,
                    float(data["min_price"]) if data.get("min_price") is not None else None,
                    float(data["clob_best_ask"]) if data.get("clob_best_ask") is not None else None,
                    float(data["clob_best_bid"]) if data.get("clob_best_bid") is not None else None,
                    data.get("execution_mode", "FOK"),
                    data.get("fok_attempt_num"),
                    data.get("fok_max_attempts"),
                    data.get("status", "submitted"),
                    float(data["fill_price"]) if data.get("fill_price") is not None else None,
                    float(data["fill_size"]) if data.get("fill_size") is not None else None,
                    data.get("order_id"),
                    data.get("error_code"),
                    data.get("error_message"),
                    data.get("latency_ms"),
                    data.get("metadata", {})
                )
        except Exception as exc:
            log.warning("db.write_clob_execution_log_failed", error=str(exc)[:200])

    async def write_fok_ladder_attempt(self, data: dict) -> None:
        """Log individual FOK ladder attempt within an execution.

        Verbatim SQL from ``DBClient.write_fok_ladder_attempt``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO fok_ladder_attempts (
                        execution_log_id, attempt_num, attempt_price, attempt_size,
                        clob_best_ask, clob_best_bid,
                        status, fill_size, fill_price,
                        error_message, attempt_duration_ms
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
                    )
                    ON CONFLICT (execution_log_id, attempt_num) DO NOTHING
                    """,
                    data.get("execution_log_id"),
                    data.get("attempt_num"),
                    float(data["attempt_price"]) if data.get("attempt_price") is not None else None,
                    float(data["attempt_size"]) if data.get("attempt_size") is not None else None,
                    float(data["clob_best_ask"]) if data.get("clob_best_ask") is not None else None,
                    float(data["clob_best_bid"]) if data.get("clob_best_bid") is not None else None,
                    data.get("status", "attempted"),
                    float(data["fill_size"]) if data.get("fill_size") is not None else None,
                    float(data["fill_price"]) if data.get("fill_price") is not None else None,
                    data.get("error_message"),
                    data.get("attempt_duration_ms")
                )
        except Exception as exc:
            log.warning("db.write_fok_ladder_attempt_failed", error=str(exc)[:200])

    async def write_countdown_evaluation(self, data: dict) -> None:
        """Persist a multi-stage countdown snapshot.

        Verbatim SQL from ``DBClient.write_countdown_evaluation``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO countdown_evaluations
                       (window_ts, stage, direction, confidence, agreement, action, notes, evaluated_at,
                        chainlink_price, tiingo_price, binance_price)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8, $9, $10)""",
                    int(data.get("window_ts", 0)),
                    data.get("stage", ""),
                    data.get("direction"),
                    float(data.get("confidence", 0)) if data.get("confidence") is not None else None,
                    bool(data.get("agreement")) if data.get("agreement") is not None else None,
                    data.get("action"),
                    data.get("notes"),
                    float(data.get("chainlink_price")) if data.get("chainlink_price") is not None else None,
                    float(data.get("tiingo_price")) if data.get("tiingo_price") is not None else None,
                    float(data.get("binance_price")) if data.get("binance_price") is not None else None,
                )
        except Exception as exc:
            log.debug("db.write_countdown_evaluation_failed", error=str(exc)[:120])

    async def write_evaluation(self, data: dict) -> None:
        """Write a Claude evaluation (compatibility shim).

        Verbatim from ``DBClient.write_evaluation``.
        """
        await self.write_countdown_evaluation({
            "window_ts": int(data.get("timestamp", 0)),
            "stage": "claude_eval",
            "direction": data.get("direction"),
            "confidence": data.get("confidence"),
            "agreement": data.get("trade_placed"),
            "action": "TRADE" if data.get("trade_placed") else "SKIP",
            "notes": data.get("analysis", "")[:2000] if data.get("analysis") else None,
        })
