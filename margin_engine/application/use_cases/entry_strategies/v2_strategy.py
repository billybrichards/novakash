"""
Legacy v2 entry strategy - probability-based with regime filter.
"""

import logging
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    CompositeSignal,
    Money,
    StopLevel,
    TradeSide,
)
from margin_engine.adapters.signal.v4_models import V4Snapshot

from .base import EntryStrategy

logger = logging.getLogger(__name__)


class V2Strategy(EntryStrategy):
    """
    Legacy v2 entry strategy from PR #10.

    Reads a single ProbabilitySignal scalar, applies soft composite
    regime filter, trades with hardcoded SL/TP.
    """

    def __init__(
        self,
        exchange,
        portfolio,
        repository,
        alerts,
        probability_port,
        signal_port,
        *,
        min_conviction: float = 0.20,
        regime_threshold: float = 0.50,
        regime_timescale: str = "1h",
        bet_fraction: float = 0.02,
        stop_loss_pct: float = 0.006,
        take_profit_pct: float = 0.005,
        venue: str = "binance",
        strategy_version: str = "v2-probability",
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._probability_port = probability_port
        self._signal_port = signal_port
        self._min_conviction = min_conviction
        self._regime_threshold = regime_threshold
        self._regime_timescale = regime_timescale
        self._bet_fraction = bet_fraction
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._venue = venue
        self._strategy_version = strategy_version

    async def evaluate(self, v4: V4Snapshot) -> Optional[Position]:
        """Execute legacy v2 entry strategy."""
        # 1. Probability freshness
        prob = await self._probability_port.get_latest(asset="BTC", timescale="15m")
        if prob is None:
            logger.debug("No fresh probability available — skipping tick")
            return None

        # 0. Window dedupe
        if (
            hasattr(self, "_last_traded_window_close_ts")
            and self._last_traded_window_close_ts is not None
            and prob.window_close_ts == self._last_traded_window_close_ts
        ):
            return None

        # 2. Conviction threshold
        if not prob.meets_threshold(self._min_conviction):
            logger.info(
                "Probability below conviction threshold: p_up=%.3f "
                "conviction=%.3f < %.3f",
                prob.probability_up,
                prob.conviction,
                self._min_conviction,
            )
            return None

        # 3. Regime filter (v3 composite magnitude) — SOFT
        regime_signal = await self._signal_port.get_latest_signal(
            self._regime_timescale
        )
        regime_strength = regime_signal.strength if regime_signal else None
        if self._regime_threshold > 0.0:
            if regime_signal is None:
                logger.info(
                    "Regime data missing (%s composite) — skipping tick",
                    self._regime_timescale,
                )
                return None
            if regime_signal.strength < self._regime_threshold:
                logger.info(
                    "Regime filter blocked: |composite_%s|=%.3f < %.3f "
                    "(p_up=%.3f would have traded)",
                    self._regime_timescale,
                    regime_signal.strength,
                    self._regime_threshold,
                    prob.probability_up,
                )
                return None

        # 4. Compute size and run risk gates
        balance = await self._exchange.get_balance()
        collateral = Money.usd(balance.amount * self._bet_fraction)
        requested_notional = collateral * self._portfolio.leverage

        allowed, reason = self._portfolio.can_open_position(collateral)
        if not allowed:
            logger.info("Position blocked by risk gate: %s", reason)
            return None

        side = prob.suggested_side

        # 5. Create + fill
        position = Position(
            asset=prob.asset,
            side=side,
            leverage=self._portfolio.leverage,
            entry_signal_score=prob.probability_up,
            entry_timescale=prob.timescale,
            venue=self._venue,
            strategy_version=self._strategy_version,
        )
        self._portfolio.add_position(position)

        try:
            fill = await self._exchange.place_market_order(
                symbol=f"{prob.asset}USDT",
                side=side,
                notional=requested_notional,
            )

            actual_notional = (
                Money.usd(fill.filled_notional)
                if fill.filled_notional > 0
                else requested_notional
            )

            position.confirm_entry(
                price=fill.fill_price,
                notional=actual_notional,
                collateral=collateral,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )

            position.stop_loss = self._compute_stop_loss(fill.fill_price, side)
            position.take_profit = self._compute_take_profit(fill.fill_price, side)

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            self._last_traded_window_close_ts = prob.window_close_ts

            logger.info(
                "Position opened (v2 ML): %s %s @ %.2f notional=%.2f "
                "commission=%.4f p_up=%.3f regime=%s",
                side.value,
                prob.asset,
                fill.fill_price.value,
                actual_notional.amount,
                fill.commission,
                prob.probability_up,
                f"{regime_strength:.3f}" if regime_strength is not None else "n/a",
            )
            return position

        except Exception as e:
            logger.error("Order placement failed: %s", e)
            await self._alerts.send_error(f"Order failed: {e}")
            if position in self._portfolio.positions:
                self._portfolio.positions.remove(position)
            return None

    def _compute_stop_loss(self, price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 - self._stop_loss_pct))
        else:
            return StopLevel(price=price.value * (1 + self._stop_loss_pct))

    def _compute_take_profit(self, price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 + self._take_profit_pct))
        else:
            return StopLevel(price=price.value * (1 - self._take_profit_pct))
