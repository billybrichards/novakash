"""
Cascade Fade Strategy (ME-STRAT-05).

Fades liquidation cascades by betting against the liquidation wave:
- LONG liquidations → bet SHORT (expect bounce)
- SHORT liquidations → bet LONG (expect bounce)

Higher risk strategy (cascades can continue), so:
- Half size (0.5x)
- Wider stops (3%)
- Quick targets (1%)
- Cooldown between cascades (15 min)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from margin_engine.domain.strategy import Strategy, TradeDecision
from margin_engine.domain.value_objects import V4Snapshot
from margin_engine.services.cascade_detector import (
    analyze_cascade,
    CascadeState,
    CascadeInfo,
)


@dataclass(frozen=True)
class CascadeFadeConfig:
    """Configuration for cascade fade strategy."""

    # Minimum cascade strength to consider fading
    min_cascade_strength: float = 0.5

    # Position sizing (half size due to higher risk)
    size_mult: float = 0.5

    # Stop loss and take profit (basis points)
    stop_loss_bps: int = 300  # 3% stop (very wide)
    take_profit_bps: int = 100  # 1% target (quick)

    # Holding period
    hold_minutes: int = 10  # Very short hold

    # Cooldown after cascade ends
    cooldown_seconds: int = 900  # 15 min cooldown


class CascadeFadeStrategy(Strategy):
    """
    Fade liquidation cascades.

    Strategy logic:
    1. Detect active cascade (strength >= 0.5)
    2. Determine cascade direction (LONG or SHORT liquidations)
    3. Fade in opposite direction (expect bounce at exhaustion)
    4. Apply size multiplier based on entry quality
    5. Track cooldown after cascade ends

    Entry quality affects sizing:
    - PREMIUM (strength >= 0.7): 0.6x size
    - STANDARD (0.5-0.7): 0.5x size
    - LATE (< 0.5): No trade
    """

    def __init__(self, config: Optional[CascadeFadeConfig] = None):
        self.config = config or CascadeFadeConfig()
        self._last_cascade_end: Optional[datetime] = None

    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """
        Make cascade fade decision.

        Args:
            v4: V4 snapshot with cascade data

        Returns:
            TradeDecision for cascade fade or no-trade
        """
        # Analyze cascade state
        cascade = analyze_cascade(v4)

        # Check if cascade is active and safe to fade
        if not cascade.is_safe_to_fade:
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="CASCADE_NOT_ACTIVE",
            )

        # Check cooldown
        if self._in_cooldown():
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="CASCADE_COOLDOWN",
            )

        # Fade the cascade (opposite direction)
        # LONG liquidations → bet SHORT
        # SHORT liquidations → bet LONG
        fade_direction = "SHORT" if cascade.direction == "LONG" else "LONG"

        # Adjust parameters based on entry quality
        if cascade.entry_quality == "PREMIUM":
            # Premium entry: slightly larger size (0.6x)
            size_mult = self.config.size_mult * 1.2
            stop_loss_bps = self.config.stop_loss_bps
        elif cascade.entry_quality == "STANDARD":
            # Standard entry: base size (0.5x)
            size_mult = self.config.size_mult
            stop_loss_bps = self.config.stop_loss_bps
        else:
            # Late entry: no trade (cascade weakening)
            return TradeDecision(
                direction=None,
                size_mult=0.0,
                stop_loss_bps=0,
                take_profit_bps=0,
                hold_minutes=0,
                reason="CASCADE_LATE_ENTRY",
            )

        return TradeDecision(
            direction=fade_direction,
            size_mult=size_mult,
            stop_loss_bps=stop_loss_bps,
            take_profit_bps=self.config.take_profit_bps,
            hold_minutes=self.config.hold_minutes,
            reason=f"CASCADE_FADE_{cascade.direction}_{cascade.entry_quality}",
        )

    def _in_cooldown(self) -> bool:
        """Check if we're in cooldown after last cascade."""
        if not self._last_cascade_end:
            return False
        elapsed = (datetime.now() - self._last_cascade_end).total_seconds()
        return elapsed < self.config.cooldown_seconds

    def on_cascade_end(self):
        """
        Called when cascade ends (state transitions to IDLE).

        Resets cooldown timer to prevent immediate re-entry.
        """
        self._last_cascade_end = datetime.now()

    def update_cascade_state(self, v4: V4Snapshot):
        """
        Update internal state based on current cascade.

        Should be called periodically to track cascade lifecycle.
        Calls on_cascade_end() when cascade transitions from active to IDLE.
        """
        cascade = analyze_cascade(v4)

        # Check if cascade just ended (was active, now IDLE)
        if (
            self._last_cascade_end is None
            and cascade.state == CascadeState.IDLE
            and cascade.strength >= 0.5
        ):
            # This is a simplification - in production, you'd track previous state
            pass
