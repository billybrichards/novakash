"""
Use case: Open a new margin position — v2 strategy (ML-directed).

v2 (April 2026) differs from v1 in three fundamental ways:

1. DIRECTION comes from a calibrated ML classifier (ProbabilitySignal), not
   from the sign of the v3 composite. The classifier was trained to forecast
   "will this 15m window close above its open" on Polymarket outcomes, and
   forward-return analysis on 5 days of historical predictions showed:
     - p > 0.70 → 77.5% hit rate, +12.82 bps avg return (maker-profitable)
     - p > 0.75 → 96.87% hit rate, +21.91 bps avg return (taker-profitable)
     - p < 0.30 → 87.5% short hit rate (maker-profitable even in up markets)

2. The v3 COMPOSITE is used as a regime filter only. We require
   |composite_1h| > regime_threshold before considering any trade — the
   intuition being that when the composite is quiet the market isn't
   moving enough to clear the fee wall even with a directionally correct
   ML call. We do NOT use the composite's sign for direction; that was
   the fee-cost trap of the v1 strategy.

3. We never enter the same 15m window twice. window_close_ts is stored
   after a successful entry and subsequent ticks whose window_close_ts
   matches are skipped. This caps the trade rate at 4 trades/hour max
   (one per 15m window) and prevents oscillation-driven re-entries.

Entry gate (evaluated in order, first failure returns None):
  0. Already traded this window?           → skip
  1. Probability freshness < 2min?         → fail if stale/missing
  2. |p_up - 0.5| >= min_conviction?       → fail if below threshold
  3. |composite_1h| >= regime_threshold?   → fail if market quiet
  4. Portfolio risk gates pass?            → standard checks
  5. Order fills successfully              → otherwise rollback
"""
from __future__ import annotations

import logging
from typing import Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    PositionRepository,
    ProbabilityPort,
    SignalPort,
)
from margin_engine.domain.value_objects import (
    CompositeSignal,
    Money,
    Price,
    ProbabilitySignal,
    StopLevel,
    TradeSide,
)

logger = logging.getLogger(__name__)


class OpenPositionUseCase:
    """
    ML-directed entry logic. See module docstring for the rationale.

    Dependencies are all domain ports; adapters are injected at wire time.
    """

    def __init__(
        self,
        exchange: ExchangePort,
        portfolio: Portfolio,
        repository: PositionRepository,
        alerts: AlertPort,
        probability_port: ProbabilityPort,
        signal_port: SignalPort,
        *,
        min_conviction: float = 0.20,       # |p-0.5| >= 0.20 → p>0.70 or p<0.30
        regime_threshold: float = 0.50,     # |composite_1h| >= 0.50 to trade
        regime_timescale: str = "1h",       # composite horizon for regime gate
        bet_fraction: float = 0.02,
        stop_loss_pct: float = 0.006,       # 0.6% — 3x fee cost
        take_profit_pct: float = 0.005,     # 0.5% — 2.7x fee cost
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

        # Track which 15m window we most recently traded in, so we don't
        # re-enter the same window if the poller sees the signal again.
        self._last_traded_window_close_ts: Optional[int] = None

    async def execute(self) -> Optional[Position]:
        """
        Evaluate current state and open a position if all gates pass.

        Note: unlike v1, this use case doesn't take a signal parameter.
        It's called on each main-loop tick and fetches the freshest
        prediction internally. This inversion means the main loop doesn't
        need to know about ProbabilityPort vs SignalPort — it just calls
        execute() periodically.
        """
        # ── 1. Probability freshness ──
        prob = await self._probability_port.get_latest(asset="BTC", timescale="15m")
        if prob is None:
            logger.debug("No fresh probability available — skipping tick")
            return None

        # ── 0. Window dedupe ──
        if (
            self._last_traded_window_close_ts is not None
            and prob.window_close_ts == self._last_traded_window_close_ts
        ):
            return None

        # ── 2. Conviction threshold ──
        if not prob.meets_threshold(self._min_conviction):
            logger.info(
                "Probability below conviction threshold: p_up=%.3f "
                "conviction=%.3f < %.3f",
                prob.probability_up, prob.conviction, self._min_conviction,
            )
            return None

        # ── 3. Regime filter (v3 composite magnitude) — SOFT ──
        # When regime_threshold is 0.0 this block only LOGS the composite
        # reading for post-hoc analysis; it does not block trading. Once
        # we have more regime-diverse data and can prove the gate adds
        # edge, bump regime_threshold above 0.0 to activate it.
        regime_signal = await self._signal_port.get_latest_signal(self._regime_timescale)
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
                    self._regime_timescale, regime_signal.strength,
                    self._regime_threshold, prob.probability_up,
                )
                return None

        # ── 4. Compute size and run risk gates ──
        balance = await self._exchange.get_balance()
        collateral = Money.usd(balance.amount * self._bet_fraction)
        requested_notional = collateral * self._portfolio.leverage

        allowed, reason = self._portfolio.can_open_position(collateral)
        if not allowed:
            logger.info("Position blocked by risk gate: %s", reason)
            return None

        side = prob.suggested_side

        # ── 5. Create + fill ──
        position = Position(
            asset=prob.asset,
            side=side,
            leverage=self._portfolio.leverage,
            # Store the probability as the "entry score" for audit — this
            # matches the schema's entry_signal_score column but now
            # represents a calibrated probability, not a composite blend.
            entry_signal_score=prob.probability_up,
            entry_timescale=prob.timescale,
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

            # Stops derived from the actual fill, not the pre-order estimate.
            position.stop_loss = self._compute_stop_loss(fill.fill_price, side)
            position.take_profit = self._compute_take_profit(fill.fill_price, side)

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            # Mark this window as traded so we don't re-enter it.
            self._last_traded_window_close_ts = prob.window_close_ts

            logger.info(
                "Position opened (v2 ML): %s %s @ %.2f notional=%.2f "
                "commission=%.4f p_up=%.3f regime=%s",
                side.value, prob.asset, fill.fill_price.value,
                actual_notional.amount, fill.commission, prob.probability_up,
                f"{regime_strength:.3f}" if regime_strength is not None else "n/a",
            )
            return position

        except Exception as e:
            logger.error("Order placement failed: %s", e)
            await self._alerts.send_error(f"Order failed: {e}")
            if position in self._portfolio.positions:
                self._portfolio.positions.remove(position)
            return None

    def _compute_stop_loss(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 - self._stop_loss_pct))
        else:
            return StopLevel(price=price.value * (1 + self._stop_loss_pct))

    def _compute_take_profit(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 + self._take_profit_pct))
        else:
            return StopLevel(price=price.value * (1 - self._take_profit_pct))
