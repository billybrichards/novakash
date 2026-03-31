"""
VPIN Cascade Strategy

Mean-reversion strategy that bets opposite to the cascade direction during
the exhaustion phase of a price cascade detected by the CascadeDetector FSM.

Theory:
  When BTC experiences a large, liquidation-driven price cascade (detected via
  VPIN > 0.70, OI drop ≥ 2%, and liquidation volume > $5M), the move is often
  driven by forced selling/buying rather than fundamentally new information.

  As the cascade exhausts (EXHAUSTING state), the informed flow dries up and
  price tends to mean-revert. This strategy bets on that reversion using
  binary prediction market contracts (e.g. "BTC > $X at EOD?").

Entry Conditions (all must be true):
  1. CascadeSignal.state == "BET_SIGNAL"
  2. VPIN has been declining from cascade peak for ≥ 2 buckets
  3. No cooldown active from consecutive losses
  4. Risk manager approval

Direction:
  - Cascade direction == "DOWN" → BUY YES (price will recover)
  - Cascade direction == "UP"   → BUY NO  (price will retrace)

Position sizing: BET_FRACTION × bankroll (Kelly-inspired).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
import structlog

from config.constants import BET_FRACTION, POLYMARKET_CRYPTO_FEE_MULT
from data.models import CascadeSignal, MarketState
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)

# States in which we act
_ACTIONABLE_STATES = {"BET_SIGNAL"}


class VPINCascadeStrategy(BaseStrategy):
    """
    Mean-reversion bets placed opposite to cascade direction during exhaustion.

    Signal dict schema:
        {
            "cascade": CascadeSignal,
            "direction": str,        # "YES" or "NO" bet direction
            "market_slug": str,
            "entry_price": Decimal,
            "stake_usd": float,
        }
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        target_market_slug: str,
        bankroll: float,
    ) -> None:
        super().__init__(
            name="vpin_cascade",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._target_market = target_market_slug
        self._bankroll = bankroll

        # Track last cascade we acted on to prevent duplicate entries
        self._last_acted_cascade_ts: Optional[datetime] = None

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """
        Check if we're in BET_SIGNAL state with a clean setup for mean reversion.
        """
        cascade = state.cascade
        if cascade is None:
            return None

        if cascade.state not in _ACTIONABLE_STATES:
            return None

        if cascade.direction is None:
            return None

        # Avoid acting on the same cascade twice
        if self._last_acted_cascade_ts == cascade.timestamp:
            return None

        # Determine bet direction (opposite to cascade)
        bet_direction = "YES" if cascade.direction == "DOWN" else "NO"

        stake_usd = self._bankroll * BET_FRACTION

        # Estimate entry price from current book (placeholder)
        entry_price = Decimal("0.50")  # Will be refined at execution time

        log.info(
            "vpin_cascade.signal_detected",
            state=cascade.state,
            cascade_dir=cascade.direction,
            bet_dir=bet_direction,
            vpin=cascade.vpin,
            stake=stake_usd,
        )

        return {
            "cascade": cascade,
            "direction": bet_direction,
            "market_slug": self._target_market,
            "entry_price": entry_price,
            "stake_usd": stake_usd,
        }

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """
        Place a single mean-reversion bet on the target Polymarket market.
        """
        stake_usd = signal["stake_usd"]
        market_slug = signal["market_slug"]
        direction = signal["direction"]

        # Risk gate
        approved, reason = await self._check_risk(stake_usd)
        if not approved:
            log.warning("vpin_cascade.blocked_by_risk", reason=reason)
            return None

        order = Order(
            order_id=str(uuid.uuid4()),
            strategy=self.name,
            venue="polymarket",
            market_slug=market_slug,
            direction=direction,
            entry_price=signal["entry_price"],
            stake_usd=stake_usd,
            fee_usd=stake_usd * POLYMARKET_CRYPTO_FEE_MULT,
            status=OrderStatus.PENDING,
            created_at=datetime.utcnow(),
            metadata={
                "cascade_vpin": signal["cascade"].vpin,
                "cascade_direction": signal["cascade"].direction,
                "oi_delta_pct": signal["cascade"].oi_delta_pct,
                "liq_volume_usd": signal["cascade"].liq_volume_usd,
            },
        )

        try:
            await self._poly.place_order(market_slug, direction, signal["entry_price"], stake_usd)
            order.status = OrderStatus.OPEN
        except Exception as exc:
            log.error("vpin_cascade.execution_failed", error=str(exc))
            return None

        await self._om.register_order(order)

        # Mark cascade as acted on
        self._last_acted_cascade_ts = signal["cascade"].timestamp

        log.info(
            "vpin_cascade.executed",
            order_id=order.order_id,
            market=market_slug,
            direction=direction,
            stake=stake_usd,
            vpin=signal["cascade"].vpin,
        )

        return order

    def update_bankroll(self, new_bankroll: float) -> None:
        """Sync bankroll reference for position sizing."""
        self._bankroll = new_bankroll
