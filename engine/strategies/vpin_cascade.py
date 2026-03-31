"""
VPIN Cascade Strategy

Mean-reversion strategy triggered when the CascadeDetector FSM emits
a BET_SIGNAL. Direction logic (mean reversion):
  - Cascade direction "down" (longs liquidated, price fell) → bet "YES" (price will recover)
  - Cascade direction "up"   (shorts liquidated, price rose) → bet "NO" (price will revert)

Venue preference: Opinion (lower fees, 4%) if connected; else Polymarket (7.2%).
Stake: BET_FRACTION * bankroll
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

import structlog

from config.constants import BET_FRACTION
from data.models import CascadeSignal, MarketState
from execution.opinion_client import OpinionClient
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class VPINCascadeStrategy(BaseStrategy):
    """
    Mean-reversion bets triggered by confirmed liquidation cascades.

    Waits for the CascadeDetector to reach BET_SIGNAL state before acting.
    Avoids duplicate entries by tracking the timestamp of the last executed cascade.
    Prefers Opinion over Polymarket for lower fees.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        opinion_client: OpinionClient,
    ) -> None:
        super().__init__(
            name="vpin_cascade",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._opinion = opinion_client
        self._last_cascade_ts: Optional[datetime] = None
        self._log = log.bind(strategy="vpin_cascade")

    # ─── Evaluate ─────────────────────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """
        Check whether the cascade FSM has reached BET_SIGNAL state.

        Only acts when:
        1. state.cascade is present
        2. cascade.state == "BET_SIGNAL"
        3. The cascade timestamp is different from the last executed one
           (prevents duplicate entries on the same signal)

        Returns a signal dict with direction and cascade details, or None.
        """
        cascade: Optional[CascadeSignal] = state.cascade
        if cascade is None:
            return None

        if cascade.state != "BET_SIGNAL":
            return None

        # Dedup: skip if we already acted on this exact cascade timestamp
        if self._last_cascade_ts is not None and cascade.timestamp == self._last_cascade_ts:
            return None

        if cascade.direction is None:
            self._log.warning("cascade.no_direction", state=cascade.state)
            return None

        # Mean reversion: cascade down → bet YES, cascade up → bet NO
        bet_direction = "YES" if cascade.direction == "down" else "NO"

        self._log.info(
            "cascade.signal",
            cascade_direction=cascade.direction,
            bet_direction=bet_direction,
            vpin=cascade.vpin,
            oi_delta=cascade.oi_delta_pct,
            liq_volume=cascade.liq_volume_usd,
        )

        return {
            "direction": bet_direction,
            "cascade": cascade,
        }

    # ─── Execute ──────────────────────────────────────────────────────────────

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """
        Place a directional mean-reversion bet.

        Venue selection:
          - Use Opinion if connected (lower 4% fees)
          - Fall back to Polymarket (7.2% fees)

        1. Compute stake from risk manager bankroll.
        2. Call risk gate.
        3. Get current market slug / ID.
        4. Place order.
        5. Register with order manager.
        6. Track cascade timestamp to avoid duplicates.
        """
        direction: str = signal["direction"]
        cascade: CascadeSignal = signal["cascade"]

        # Determine stake
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        stake = BET_FRACTION * bankroll

        # Risk gate
        approved, reason = await self._check_risk(stake)
        if not approved:
            self._log.info("cascade.risk_blocked", reason=reason, direction=direction)
            return None

        # Venue selection: prefer Opinion for lower fees
        use_opinion = self._opinion.connected

        order_id: Optional[str] = None
        venue: str

        if use_opinion:
            venue = "opinion"
            market_id = self._poly.get_current_market_slug()  # reuse slug as market ID
            # Get estimated price from polymarket for reference
            try:
                prices = await self._poly.get_market_prices(market_id)
                price = prices.get(direction.lower(), Decimal("0.5"))
            except Exception:
                price = Decimal("0.5")

            try:
                order_id = await self._opinion.place_order(
                    market_id=market_id,
                    direction=direction,
                    price=price,
                    stake_usd=stake,
                )
            except Exception as exc:
                self._log.error("cascade.opinion_failed", error=str(exc))
                # Fall back to Polymarket
                use_opinion = False

        if not use_opinion:
            venue = "polymarket"
            market_slug = self._poly.get_current_market_slug()
            try:
                prices = await self._poly.get_market_prices(market_slug)
                price = prices.get(direction.lower(), Decimal("0.5"))
            except Exception:
                price = Decimal("0.5")

            try:
                order_id = await self._poly.place_order(
                    market_slug=market_slug,
                    direction=direction,
                    price=price,
                    stake_usd=stake,
                )
            except Exception as exc:
                self._log.error("cascade.polymarket_failed", error=str(exc))
                return None

        if order_id is None:
            self._log.error("cascade.no_order_id", direction=direction)
            return None

        # Compute fee based on venue
        from config.constants import OPINION_CRYPTO_FEE_MULT, POLYMARKET_CRYPTO_FEE_MULT
        fee_mult = OPINION_CRYPTO_FEE_MULT if venue == "opinion" else POLYMARKET_CRYPTO_FEE_MULT
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake

        market_slug = self._poly.get_current_market_slug()

        btc_price = float(state.btc_price) if state.btc_price else None
        window_s = 900 if venue == "opinion" else 300

        order = Order(
            order_id=f"cascade-{uuid.uuid4().hex[:12]}",
            strategy=self.name,
            venue=venue,
            direction=direction,
            price=str(price),
            stake_usd=stake,
            fee_usd=fee_usd,
            status=OrderStatus.OPEN,
            btc_entry_price=btc_price,
            window_seconds=window_s,
            market_id=market_slug,
            metadata={
                "venue_order_id": order_id,
                "cascade_direction": cascade.direction,
                "cascade_vpin": cascade.vpin,
                "cascade_oi_delta_pct": cascade.oi_delta_pct,
                "cascade_liq_volume_usd": cascade.liq_volume_usd,
            },
        )

        await self._om.register_order(order)

        # Track cascade timestamp to prevent duplicate entries
        self._last_cascade_ts = cascade.timestamp

        self._log.info(
            "cascade.executed",
            order_id=order.order_id,
            venue=venue,
            direction=direction,
            stake=stake,
            price=str(price),
        )

        return order
