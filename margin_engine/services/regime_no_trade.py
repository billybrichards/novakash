"""
No-trade strategy for CHOPPY and NO_EDGE regimes.

Safest approach: don't trade when there's no clear edge.
"""

from typing import Optional

from margin_engine.domain.strategy import Strategy, TradeDecision
from margin_engine.domain.value_objects import V4Snapshot


class NoTradeStrategy(Strategy):
    """
    No-trade strategy for CHOPPY and NO_EDGE regimes.

    Logic:
    - Always return no-trade decision
    - This is the default safe behavior when market has no clear edge
    - Can be overridden via configuration if needed (e.g., very small size)
    """

    def __init__(self, allow_trade: bool = False, size_mult: float = 0.1):
        """
        Initialize no-trade strategy.

        Args:
            allow_trade: If True, allow tiny positions (default False)
            size_mult: Size multiplier if allow_trade is True (default 0.1)
        """
        self._allow_trade = allow_trade
        self._size_mult = size_mult

    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """
        Always return no-trade decision.

        Args:
            v4: V4 snapshot (ignored)

        Returns:
            TradeDecision with direction=None
        """
        if self._allow_trade:
            # Allow tiny speculative positions (10% size)
            return TradeDecision(
                direction=None,
                size_mult=self._size_mult,
                stop_loss_bps=50,  # Very tight stops
                take_profit_bps=30,  # Quick profit
                hold_minutes=5,  # Very short hold
                reason="CHOPPY_SPECULATIVE",
            )

        # Default: no trade
        return TradeDecision(
            direction=None,
            size_mult=0.0,
            stop_loss_bps=0,
            take_profit_bps=0,
            hold_minutes=0,
            reason="REGIME_NO_TRADE",
        )
