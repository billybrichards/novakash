"""
Strategy pattern for regime-adaptive trading.

Defines the base Strategy ABC, Regime enum, and TradeDecision dataclass
that concrete strategies implement based on market regime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from enum import Enum


class Regime(Enum):
    """Market regime classification from V4 snapshot."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    MEAN_REVERTING = "MEAN_REVERTING"
    CHOPPY = "CHOPPY"
    NO_EDGE = "NO_EDGE"


@dataclass(frozen=True)
class TradeDecision:
    """
    Immutable trading decision from a strategy.

    All fields are set even when direction=None (no trade) for consistent logging.
    """

    direction: Optional[str]  # "LONG", "SHORT", or None (no trade)
    size_mult: float  # Position size multiplier (default 1.0)
    stop_loss_bps: int  # Stop loss in basis points (e.g., 150 = 1.5%)
    take_profit_bps: int  # Take profit in basis points (e.g., 200 = 2.0%)
    hold_minutes: int  # Expected holding period in minutes
    reason: (
        str  # Why this decision (e.g., "TREND_LONG", "FADE_SHORT", "REGIME_NO_TRADE")
    )

    @property
    def is_trade(self) -> bool:
        """True if this decision recommends entering a position."""
        return self.direction is not None

    @property
    def stop_loss_pct(self) -> float:
        """Stop loss as a percentage."""
        return self.stop_loss_bps / 10_000.0

    @property
    def take_profit_pct(self) -> float:
        """Take profit as a percentage."""
        return self.take_profit_bps / 10_000.0

    @property
    def reward_risk_ratio(self) -> float:
        """Reward-to-risk ratio (TP/SL)."""
        if self.stop_loss_bps <= 0:
            return 0.0
        return self.take_profit_bps / self.stop_loss_bps


class Strategy:
    """
    Base class for regime-specific trading strategies.

    Concrete strategies implement market logic for their regime type.
    """

    def decide(self, v4: "V4Snapshot") -> TradeDecision:
        """
        Make a trading decision based on V4 snapshot data.

        Args:
            v4: V4 snapshot containing regime, probability, quantiles, etc.

        Returns:
            TradeDecision with direction, sizing, stops, and rationale.

        Note:
            Subclasses MUST override this method. The default implementation
            always returns no-trade for safety.
        """
        # Default: no trade (safe fallback)
        return TradeDecision(
            direction=None,
            size_mult=0.0,
            stop_loss_bps=0,
            take_profit_bps=0,
            hold_minutes=0,
            reason="STRATEGY_NOT_IMPLEMENTED",
        )
