"""
Trend-following strategy for TRENDING_UP and TRENDING_DOWN regimes.

Larger positions, wider stops, longer holds to capture sustained moves.
"""

from dataclasses import dataclass
from typing import Optional

from margin_engine.application.services.strategy import Strategy, TradeDecision
from margin_engine.adapters.signal.v4_models import V4Snapshot


@dataclass(frozen=True)
class TrendStrategyConfig:
    """Configuration for trend-following strategy."""

    # Entry threshold: minimum probability to enter
    min_probability: float = 0.55

    # Position sizing
    size_mult: float = 1.2  # 20% larger positions in trending regimes

    # Stop loss and take profit (basis points)
    stop_loss_bps: int = 150  # 1.5% wider stops
    take_profit_bps: int = 200  # 2.0% target

    # Holding period
    hold_minutes: int = 60  # Hold for 1 hour in trends

    # Trailing stop enabled
    trailing_stop: bool = True

    # Minimum expected move to consider (bps)
    min_expected_move_bps: float = 30.0


class TrendStrategy(Strategy):
    """
    Trend-following strategy for TRENDING_UP and TRENDING_DOWN regimes.

    Logic:
    - Enter in direction of trend (p_up > 0.5 → LONG, p_up < 0.5 → SHORT)
    - Require minimum probability (p >= 0.55 for LONG, p <= 0.45 for SHORT)
    - Larger position size (1.2x)
    - Wider stops (1.5%) to avoid noise
    - Longer hold time (60 min) to capture sustained moves
    - Take profit at 2% target
    """

    def __init__(self, config: Optional[TrendStrategyConfig] = None):
        self.config = config or TrendStrategyConfig()

    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """
        Make trend-following decision based on primary timescale.

        Args:
            v4: V4 snapshot with regime classification

        Returns:
            TradeDecision for trend entry or no-trade
        """
        # Get primary timescale (default 15m)
        ts = v4.timescales.get("15m")
        if ts is None:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="PRIMARY_TIMESCALE_MISSING",
            )

        # Get probability
        p_up = ts.probability_up
        if p_up is None:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="PROBABILITY_MISSING",
            )

        # Check regime - only trade if trending (before other checks)
        if ts.regime not in ("TRENDING_UP", "TRENDING_DOWN"):
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="NOT_TRENDING",
            )

        # Determine direction from probability
        if p_up >= 0.5:
            direction = "LONG"
        else:
            direction = "SHORT"

        # Check if probability is strong enough
        # For LONG: need p_up >= min_probability (e.g., 0.55)
        # For SHORT: need (1 - p_up) >= min_probability, i.e., p_up <= (1 - min_probability)
        if direction == "LONG" and p_up < self.config.min_probability:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="TREND_TOO_WEAK",
            )
        if direction == "SHORT" and (1 - p_up) < self.config.min_probability:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="TREND_TOO_WEAK",
            )

        # Check expected move (if available)
        if ts.expected_move_bps is not None:
            if abs(ts.expected_move_bps) < self.config.min_expected_move_bps:
                return TradeDecision(
                    direction=None,
                    size_mult=0.0,
                    stop_loss_bps=0,
                    take_profit_bps=0,
                    hold_minutes=0,
                    reason="EXPECTED_MOVE_TOO_SMALL",
                )

        # All checks passed - return trade decision
        return TradeDecision(
            direction=direction,
            size_mult=self.config.size_mult,
            stop_loss_bps=self.config.stop_loss_bps,
            take_profit_bps=self.config.take_profit_bps,
            hold_minutes=self.config.hold_minutes,
            reason=f"TREND_{direction}",
        )
