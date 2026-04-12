"""
Mean-reversion strategy for MEAN_REVERTING regime.

Fade extremes with smaller positions, tighter stops, quick profits.
"""

from dataclasses import dataclass
from typing import Optional

from margin_engine.domain.strategy import Strategy, TradeDecision
from margin_engine.domain.value_objects import V4Snapshot


@dataclass(frozen=True)
class MeanReversionConfig:
    """Configuration for mean-reversion strategy."""

    # Entry threshold: wait for extreme probability (70%+ or 30%-)
    entry_threshold: float = 0.70

    # Position sizing
    size_mult: float = 0.8  # 20% smaller positions (fade is riskier)

    # Stop loss and take profit (basis points)
    stop_loss_bps: int = 80  # 0.8% tighter stops
    take_profit_bps: int = 50  # 0.5% quick profit target

    # Holding period
    hold_minutes: int = 15  # Quick 15-minute hold

    # Minimum probability of the fade being correct (15%+ is acceptable for fade)
    min_fade_conviction: float = 0.15


class MeanReversionStrategy(Strategy):
    """
    Mean-reversion strategy for MEAN_REVERTING regime.

    Logic:
    - Fade extremes: bet against strong moves
    - p_up >= 0.70 → bet SHORT (fade bullish extreme)
    - p_up <= 0.30 → bet LONG (fade bearish extreme)
    - Smaller position size (0.8x)
    - Tighter stops (0.8%) to limit fade risk
    - Quick profit target (0.5%) for mean reversion
    - Short hold time (15 min)
    """

    def __init__(self, config: Optional[MeanReversionConfig] = None):
        self.config = config or MeanReversionConfig()

    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """
        Make mean-reversion decision based on primary timescale.

        Args:
            v4: V4 snapshot with regime classification

        Returns:
            TradeDecision for fade entry or no-trade
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

        # Check regime - only trade if mean-reverting
        if ts.regime != "MEAN_REVERTING":
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="NOT_MEAN_REVERTING",
            )

        # Determine if extreme enough to fade
        if p_up >= self.config.entry_threshold:
            # Very bullish (70%+) → bet SHORT (fade)
            direction = "SHORT"
        elif p_up <= (1 - self.config.entry_threshold):
            # Very bearish (30%-) → bet LONG (fade)
            direction = "LONG"
        else:
            # Not extreme enough to fade
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="NOT_EXTREME_ENOUGH",
            )

        # Check if strong enough to fade
        # For SHORT fade (p_up >= 0.70): need (1 - p_up) >= 0.55
        # For LONG fade (p_up <= 0.30): need (1 - p_up) >= 0.55
        if direction == "SHORT" and (1 - p_up) < self.config.min_fade_conviction:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="FADE_WEAK",
            )
        if direction == "LONG" and (1 - p_up) < self.config.min_fade_conviction:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="FADE_WEAK",
            )

        # All checks passed - return trade decision
        return TradeDecision(
            direction=direction,
            size_mult=self.config.size_mult,
            stop_loss_bps=self.config.stop_loss_bps,
            take_profit_bps=self.config.take_profit_bps,
            hold_minutes=self.config.hold_minutes,
            reason=f"FADE_{direction}",
        )
