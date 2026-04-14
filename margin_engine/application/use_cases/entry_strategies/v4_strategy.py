"""
V4 entry strategy - 10-gate decision stack.
"""

import logging
from typing import TYPE_CHECKING, Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    Money,
    StopLevel,
    TradeSide,
    V4Snapshot,
    TimescalePayload,
)

from .base import EntryStrategy

if TYPE_CHECKING:
    from margin_engine.domain.entities.portfolio import Portfolio
    from margin_engine.domain.ports import (
        AlertPort,
        ExchangePort,
        PositionRepository,
    )

logger = logging.getLogger(__name__)


class V4Strategy(EntryStrategy):
    """
    V4 entry strategy with 10-gate decision stack.

    Gate order (cheapest first):
      ①  primary timescale tradeable?
      ②  consensus.safe_to_trade
      ③  macro.direction_gate permits side
      ④  minutes_to_next_high_impact >= 30
      ⑤  regime != MEAN_REVERTING or opt-in
      ⑥  |p_up - 0.5| >= v4_entry_edge
      ⑦  |expected_move_bps| >= fee wall
      ⑧  portfolio.can_open_position
      ⑨  balance query
      ⑩  SL/TP from quantiles + reward/risk
    """

    def __init__(
        self,
        exchange: "ExchangePort",
        portfolio: "Portfolio",
        repository: "PositionRepository",
        alerts: "AlertPort",
        *,
        # v4 config
        v4_primary_timescale: str = "15m",
        v4_entry_edge: float = 0.10,
        v4_min_expected_move_bps: float = 15.0,
        v4_allow_mean_reverting: bool = False,
        # macro mode
        v4_macro_mode: str = "advisory",
        v4_macro_hard_veto_confidence_floor: int = 80,
        v4_macro_advisory_size_mult_on_conflict: float = 0.75,
        # NO_EDGE override
        v4_allow_no_edge_if_exp_move_bps_gte: Optional[float] = None,
        # DQ-07 mark divergence
        v4_max_mark_divergence_bps: float = 0.0,
        fee_rate_per_side: float = 0.00045,
        # regime adaptive
        regime_adaptive_enabled: bool = False,
        regime_trend_min_prob: float = 0.55,
        regime_trend_size_mult: float = 1.2,
        regime_trend_stop_bps: int = 150,
        regime_trend_tp_bps: int = 200,
        regime_trend_hold_minutes: int = 60,
        regime_trend_min_expected_move_bps: float = 30.0,
        regime_mr_entry_threshold: float = 0.70,
        regime_mr_size_mult: float = 0.8,
        regime_mr_stop_bps: int = 80,
        regime_mr_tp_bps: int = 50,
        regime_mr_hold_minutes: int = 15,
        regime_mr_min_fade_conviction: float = 0.55,
        regime_no_trade_allow: bool = False,
        regime_no_trade_size_mult: float = 0.1,
        # common
        bet_fraction: float = 0.02,
        stop_loss_pct: float = 0.006,
        take_profit_pct: float = 0.005,
        venue: str = "binance",
        strategy_version: str = "v4",
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._bet_fraction = bet_fraction

        # v4 config
        self._v4_primary_timescale = v4_primary_timescale
        self._v4_entry_edge = v4_entry_edge
        self._v4_min_expected_move_bps = v4_min_expected_move_bps
        self._v4_allow_mean_reverting = v4_allow_mean_reverting
        self._allow_no_edge_exp_move_override = v4_allow_no_edge_if_exp_move_bps_gte
        self._macro_mode = v4_macro_mode
        self._macro_hard_veto_confidence_floor = v4_macro_hard_veto_confidence_floor
        self._macro_advisory_conflict_mult = v4_macro_advisory_size_mult_on_conflict
        self._v4_max_mark_divergence_bps = v4_max_mark_divergence_bps
        self._fee_rate_per_side = fee_rate_per_side
        self._venue = venue
        self._strategy_version = strategy_version
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct

        # Regime adaptive
        self._regime_adaptive_enabled = regime_adaptive_enabled
        if regime_adaptive_enabled:
            from margin_engine.application.services.regime_adaptive import (
                RegimeAdaptiveRouter,
            )
            from margin_engine.application.services.regime_trend import (
                TrendStrategyConfig,
            )
            from margin_engine.application.services.regime_mean_reversion import (
                MeanReversionConfig,
            )

            self._regime_router = RegimeAdaptiveRouter(
                trend_config=TrendStrategyConfig(
                    min_probability=regime_trend_min_prob,
                    size_mult=regime_trend_size_mult,
                    stop_loss_bps=regime_trend_stop_bps,
                    take_profit_bps=regime_trend_tp_bps,
                    hold_minutes=regime_trend_hold_minutes,
                    min_expected_move_bps=regime_trend_min_expected_move_bps,
                ),
                mean_reversion_config=MeanReversionConfig(
                    entry_threshold=regime_mr_entry_threshold,
                    size_mult=regime_mr_size_mult,
                    stop_loss_bps=regime_mr_stop_bps,
                    take_profit_bps=regime_mr_tp_bps,
                    hold_minutes=regime_mr_hold_minutes,
                    min_fade_conviction=regime_mr_min_fade_conviction,
                ),
                no_trade_allow=regime_no_trade_allow,
                no_trade_size_mult=regime_no_trade_size_mult,
            )
        else:
            self._regime_router = None

    async def evaluate(self, v4: V4Snapshot) -> Optional[Position]:
        """Execute v4 entry strategy."""
        payload = v4.timescales.get(self._v4_primary_timescale)
        if payload is None:
            self._log_skip("primary_timescale_missing", v4, None)
            return None

        # Window dedupe
        if (
            hasattr(self, "_last_traded_window_close_ts")
            and self._last_traded_window_close_ts is not None
            and payload.window_close_ts == self._last_traded_window_close_ts
            and payload.window_close_ts > 0
        ):
            return None

        # Gate ①: tradeable state
        is_tradeable = payload.is_tradeable
        if (
            not is_tradeable
            and payload.regime == "NO_EDGE"
            and self._allow_no_edge_exp_move_override is not None
            and payload.expected_move_bps is not None
            and abs(payload.expected_move_bps) >= self._allow_no_edge_exp_move_override
        ):
            is_tradeable = True
            logger.info(
                "v4 entry: NO_EDGE override applied (exp_move=%.1f bps >= thr=%.1f)",
                payload.expected_move_bps,
                self._allow_no_edge_exp_move_override,
            )
        if not is_tradeable:
            self._log_skip("not_tradeable", v4, payload)
            return None

        side = payload.suggested_side

        # Gate ②: consensus
        if not v4.consensus.safe_to_trade:
            self._log_skip("consensus_fail", v4, payload)
            return None

        # Gate ③: macro direction_gate
        macro_conflict = False
        if (
            v4.macro.status == "ok"
            and v4.macro.confidence >= self._macro_hard_veto_confidence_floor
        ):
            if v4.macro.direction_gate == "SKIP_UP" and side == TradeSide.LONG:
                if self._macro_mode == "veto":
                    self._log_skip("macro_skip_up_veto", v4, payload)
                    return None
                macro_conflict = True
            elif v4.macro.direction_gate == "SKIP_DOWN" and side == TradeSide.SHORT:
                if self._macro_mode == "veto":
                    self._log_skip("macro_skip_down_veto", v4, payload)
                    return None
                macro_conflict = True

        # Gate ④: high-impact event
        if (
            v4.max_impact_in_window in ("HIGH", "EXTREME")
            and v4.minutes_to_next_high_impact is not None
            and v4.minutes_to_next_high_impact < 30
        ):
            self._log_skip("high_impact_event_within_30min", v4, payload)
            return None

        # Gate ⑤: regime opt-in
        if payload.regime == "MEAN_REVERTING" and not self._v4_allow_mean_reverting:
            self._log_skip("mean_reverting_not_allowed", v4, payload)
            return None

        # Gate ⑤.5: regime adaptive strategy
        regime_decision = None
        if self._regime_adaptive_enabled and self._regime_router is not None:
            regime_decision = self._regime_router.decide(v4)
            if not regime_decision.is_trade:
                self._log_skip(
                    f"regime_strategy_no_trade:{regime_decision.reason}",
                    v4,
                    payload,
                )
                return None
            logger.info(
                "v4 entry: regime strategy decision — %s size_mult=%.2f "
                "stop=%.1fbp tp=%.1fbp hold=%dm",
                regime_decision.reason,
                regime_decision.size_mult,
                regime_decision.stop_loss_bps,
                regime_decision.take_profit_bps,
                regime_decision.hold_minutes,
            )

        # Gate ⑥: conviction threshold
        if not payload.meets_threshold(self._v4_entry_edge):
            self._log_skip("conviction_below_threshold", v4, payload)
            return None

        # Gate ⑦: expected move clears fee wall
        if (
            payload.expected_move_bps is None
            or abs(payload.expected_move_bps) < self._v4_min_expected_move_bps
        ):
            self._log_skip("expected_move_below_fee_wall", v4, payload)
            return None

        # Gate ⑧: portfolio risk gate
        size_mult = v4.macro.size_modifier if v4.macro.status == "ok" else 1.0
        if macro_conflict:
            size_mult *= self._macro_advisory_conflict_mult
            logger.info(
                "v4 entry: macro advisory conflict — size_mult *= %.2f "
                "(final %.3f, macro=%s/%d/%s, side=%s)",
                self._macro_advisory_conflict_mult,
                size_mult,
                v4.macro.bias,
                v4.macro.confidence,
                v4.macro.direction_gate,
                side.value,
            )

        if regime_decision is not None:
            size_mult *= regime_decision.size_mult
            logger.info(
                "v4 entry: regime size mult applied — total size_mult=%.3f "
                "(macro=%.3f × regime=%.2f)",
                size_mult,
                v4.macro.size_modifier if v4.macro.status == "ok" else 1.0,
                regime_decision.size_mult,
            )

        preliminary_collateral = Money.usd(
            self._portfolio.starting_capital.amount * self._bet_fraction * size_mult
        )
        allowed, reason = self._portfolio.can_open_position(preliminary_collateral)
        if not allowed:
            logger.info("v4 entry blocked by risk gate: %s", reason)
            return None

        # Gate ⑨: balance query
        balance = await self._exchange.get_balance()
        collateral = Money.usd(
            self._portfolio.starting_capital.amount * self._bet_fraction * size_mult
        )
        requested_notional = collateral.amount * self._portfolio.leverage

        # Gate 9.5: mark divergence
        if self._v4_max_mark_divergence_bps > 0:
            exchange_mark = None
            try:
                mark_price = await self._exchange.get_mark(
                    symbol=f"{v4.asset}USDT",
                    side=side,
                )
                exchange_mark = (
                    float(mark_price.value)
                    if hasattr(mark_price, "value")
                    else float(mark_price)
                )
            except Exception as exc:
                logger.warning(
                    "dq07.mark_query_failed — graceful passthrough: %s",
                    str(exc)[:200],
                )
                exchange_mark = None

            if (
                exchange_mark is not None
                and v4.last_price is not None
                and v4.last_price > 0
            ):
                divergence_bps = (
                    abs(exchange_mark - v4.last_price) / v4.last_price * 10_000.0
                )
                if divergence_bps > self._v4_max_mark_divergence_bps:
                    logger.warning(
                        "dq07.mark_divergence_gate_failed: "
                        "v4_last_price=%.4f exchange_mark=%.4f "
                        "divergence_bps=%.2f threshold_bps=%.2f side=%s",
                        v4.last_price,
                        exchange_mark,
                        round(divergence_bps, 2),
                        self._v4_max_mark_divergence_bps,
                        side.value,
                    )
                    self._log_skip("mark_divergence", v4, payload)
                    return None

        # Gate ⑩: SL/TP from quantiles
        if regime_decision is not None:
            sl_pct = regime_decision.stop_loss_pct
            tp_pct = regime_decision.take_profit_pct
            logger.info(
                "v4 entry: regime SL/TP used — stop=%.1fbp tp=%.1fbp rr=%.2f",
                regime_decision.stop_loss_bps,
                regime_decision.take_profit_bps,
                regime_decision.reward_risk_ratio,
            )
        else:
            sl_pct, tp_pct = self._sl_tp_from_quantiles(side, payload, v4.last_price)

        fee_budget_pct = self._fee_rate_per_side * 2
        if tp_pct < fee_budget_pct * 1.3:
            self._log_skip("tp_below_fee_wall", v4, payload)
            return None
        if sl_pct <= 0 or (tp_pct / sl_pct) < 1.2:
            self._log_skip("win_ratio_below_1.2", v4, payload)
            return None

        # All gates passed - build position
        position = Position(
            asset=v4.asset,
            side=side,
            leverage=self._portfolio.leverage,
            entry_signal_score=payload.probability_up or 0.0,
            entry_timescale=self._v4_primary_timescale,
            venue=self._venue,
            strategy_version=self._strategy_version,
            v4_entry_regime=payload.regime,
            v4_entry_macro_bias=v4.macro.bias if v4.macro.status == "ok" else None,
            v4_entry_macro_confidence=v4.macro.confidence
            if v4.macro.status == "ok"
            else None,
            v4_entry_expected_move_bps=payload.expected_move_bps,
            v4_entry_composite_v3=payload.composite_v3,
            v4_entry_consensus_safe=v4.consensus.safe_to_trade,
            v4_entry_window_close_ts=payload.window_close_ts
            if payload.window_close_ts > 0
            else None,
            v4_snapshot_ts_at_entry=v4.ts,
            v4_entry_strategy_decision=regime_decision.reason
            if regime_decision
            else None,
            v4_entry_strategy_size_mult=regime_decision.size_mult
            if regime_decision
            else None,
            v4_entry_strategy_hold_minutes=regime_decision.hold_minutes
            if regime_decision
            else None,
        )
        self._portfolio.add_position(position)

        try:
            fill = await self._exchange.place_market_order(
                symbol=f"{v4.asset}USDT",
                side=side,
                notional=requested_notional,
            )

            actual_notional = (
                fill.filled_notional if fill.filled_notional > 0 else requested_notional
            )

            position.confirm_entry(
                price=fill.fill_price,
                notional=actual_notional,
                collateral=collateral,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )

            position.stop_loss = StopLevel(
                price=fill.fill_price.value
                * ((1 - sl_pct) if side == TradeSide.LONG else (1 + sl_pct))
            )
            position.take_profit = StopLevel(
                price=fill.fill_price.value
                * ((1 + tp_pct) if side == TradeSide.LONG else (1 - tp_pct))
            )

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            if payload.window_close_ts > 0:
                self._last_traded_window_close_ts = payload.window_close_ts

            logger.info(
                "Position opened (v4): %s %s @ %.2f notional=%.2f p_up=%.3f "
                "regime=%s macro=%s/%d sl=%.2fbp tp=%.2fbp rr=%.2f",
                side.value,
                v4.asset,
                fill.fill_price.value,
                actual_notional,
                payload.probability_up or 0.0,
                payload.regime,
                v4.macro.bias,
                v4.macro.confidence,
                sl_pct * 10000,
                tp_pct * 10000,
                tp_pct / sl_pct if sl_pct > 0 else 0.0,
            )
            return position

        except Exception as e:
            logger.error("v4 order placement failed: %s", e)
            await self._alerts.send_error(f"v4 order failed: {e}")
            if position in self._portfolio.positions:
                self._portfolio.positions.remove(position)
            return None

    def _sl_tp_from_quantiles(
        self,
        side: TradeSide,
        payload: TimescalePayload,
        last_price: Optional[float],
    ) -> tuple[float, float]:
        """Derive SL/TP percentages from TimesFM p10/p90 at the window close."""
        p10 = payload.quantiles_at_close.p10
        p90 = payload.quantiles_at_close.p90
        if p10 is None or p90 is None or last_price is None or last_price <= 0:
            return self._stop_loss_pct, self._take_profit_pct

        if side == TradeSide.LONG:
            sl_abs = (last_price - p10) * 1.25
            tp_abs = (p90 - last_price) * 0.85
        else:
            sl_abs = (p90 - last_price) * 1.25
            tp_abs = (last_price - p10) * 0.85

        sl_pct = max(0.002, sl_abs / last_price)
        tp_pct = max(0.003, tp_abs / last_price)
        return sl_pct, tp_pct

    def _log_skip(
        self,
        reason: str,
        v4: V4Snapshot,
        payload: Optional[TimescalePayload],
    ) -> None:
        """Structured skip log."""
        logger.info(
            "v4 entry skip: reason=%s primary_ts=%s p_up=%s regime=%s status=%s "
            "macro=%s/%s confidence=%d consensus_safe=%s "
            "expected_move=%s event=%s",
            reason,
            self._v4_primary_timescale,
            f"{payload.probability_up:.3f}"
            if payload and payload.probability_up is not None
            else "?",
            payload.regime if payload else "?",
            payload.status if payload else "?",
            v4.macro.bias,
            v4.macro.direction_gate,
            v4.macro.confidence,
            v4.consensus.safe_to_trade,
            f"{payload.expected_move_bps:.1f}"
            if payload and payload.expected_move_bps is not None
            else "?",
            v4.max_impact_in_window,
        )
