"""
Regime-adaptive router that dispatches to appropriate strategy based on regime.

Routes:
- TRENDING_UP/TRENDING_DOWN → TrendStrategy
- MEAN_REVERTING → MeanReversionStrategy
- CHOPPY/NO_EDGE → NoTradeStrategy
"""

from typing import Dict, Optional

from margin_engine.application.services.strategy import Strategy, TradeDecision, Regime
from margin_engine.domain.value_objects import V4Snapshot
from margin_engine.application.services.regime_trend import (
    TrendStrategy,
    TrendStrategyConfig,
)
from margin_engine.application.services.regime_mean_reversion import (
    MeanReversionStrategy,
    MeanReversionConfig,
)
from margin_engine.application.services.regime_no_trade import NoTradeStrategy


class RegimeAdaptiveRouter:
    """
    Route to appropriate strategy based on market regime.

    The router:
    1. Extracts regime from primary timescale
    2. Selects the appropriate strategy
    3. Delegates decision to that strategy
    4. Annotates the decision with regime context
    """

    def __init__(
        self,
        trend_config: Optional[TrendStrategyConfig] = None,
        mean_reversion_config: Optional[MeanReversionConfig] = None,
        no_trade_allow: bool = False,
        no_trade_size_mult: float = 0.1,
    ):
        """
        Initialize regime router with strategy configurations.

        Args:
            trend_config: Configuration for trend strategy
            mean_reversion_config: Configuration for mean-reversion strategy
            no_trade_allow: If True, allow speculative trades in CHOPPY/NO_EDGE
            no_trade_size_mult: Size multiplier for speculative trades
        """
        self.strategies: Dict[str, Strategy] = {
            "TRENDING_UP": TrendStrategy(trend_config),
            "TRENDING_DOWN": TrendStrategy(trend_config),
            "MEAN_REVERTING": MeanReversionStrategy(mean_reversion_config),
            "CHOPPY": NoTradeStrategy(
                allow_trade=no_trade_allow, size_mult=no_trade_size_mult
            ),
            "NO_EDGE": NoTradeStrategy(
                allow_trade=no_trade_allow, size_mult=no_trade_size_mult
            ),
        }

    def get_strategy(self, regime: str) -> Strategy:
        """
        Get strategy for a given regime.

        Args:
            regime: Regime string from V4 snapshot

        Returns:
            Appropriate strategy for the regime
        """
        return self.strategies.get(regime, NoTradeStrategy())

    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """
        Make adaptive trading decision based on regime.

        Args:
            v4: V4 snapshot containing regime classification

        Returns:
            TradeDecision from the appropriate strategy, annotated with regime
        """
        # Get regime from primary timescale
        ts = v4.timescales.get("15m")
        if ts is None or ts.regime is None:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="REGIME_UNKNOWN",
            )

        regime_str = ts.regime
        try:
            regime = Regime(regime_str)
        except ValueError:
            # Unknown regime - default to no trade
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason=f"REGIME_UNKNOWN_{regime_str}",
            )

        # Get appropriate strategy
        strategy = self.get_strategy(regime_str)

        # Make decision
        decision = strategy.decide(v4)

        # Add regime context to reason
        if decision.direction is None and decision.reason == "REGIME_NO_TRADE":
            # Keep the reason as-is for no-trade
            pass
        else:
            # Prefix with regime for logging
            decision = TradeDecision(
                direction=decision.direction,
                size_mult=decision.size_mult,
                stop_loss_bps=decision.stop_loss_bps,
                take_profit_bps=decision.take_profit_bps,
                hold_minutes=decision.hold_minutes,
                reason=f"{regime.value}_{decision.reason}",
            )

        return decision

    def get_regime(self, v4: V4Snapshot) -> Optional[str]:
        """
        Get current regime from primary timescale.

        Args:
            v4: V4 snapshot

        Returns:
            Regime string or None if unavailable
        """
        ts = v4.timescales.get("15m")
        return ts.regime if ts else None
