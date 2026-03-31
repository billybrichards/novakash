"""
Sub-Dollar Arbitrage Strategy

Detects and executes simultaneous YES+NO purchases when the combined price
is below $1.00 minus fees, guaranteeing a risk-free profit on resolution.

Fee model (per leg):
    fee_leg = POLYMARKET_CRYPTO_FEE_MULT * price * (1 - price)

Stake: min(ARB_MAX_POSITION, bankroll * BET_FRACTION)
Both legs must be executed within ARB_MAX_EXECUTION_MS (500ms).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

import structlog

from config.constants import (
    ARB_MAX_EXECUTION_MS,
    ARB_MAX_POSITION,
    ARB_MIN_SPREAD,
    BET_FRACTION,
    POLYMARKET_CRYPTO_FEE_MULT,
)
from data.models import ArbOpportunity, MarketState
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class SubDollarArbStrategy(BaseStrategy):
    """
    Sub-dollar arbitrage: buy both YES and NO when combined price < $1 after fees.

    Since YES + NO = $1 at resolution, buying both legs below $1 (net of fees)
    locks in a guaranteed profit regardless of outcome.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
    ) -> None:
        super().__init__(
            name="sub_dollar_arb",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._log = log.bind(strategy="sub_dollar_arb")

    # ─── Evaluate ─────────────────────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """
        Scan arb opportunities from state and return the best one above ARB_MIN_SPREAD.

        Returns a signal dict with the selected ArbOpportunity if one qualifies,
        or None if no opportunity meets the minimum spread threshold.
        """
        if not state.arb_opportunities:
            return None

        # Filter opportunities above minimum net spread
        qualified = [
            opp for opp in state.arb_opportunities
            if float(opp.net_spread) >= ARB_MIN_SPREAD
        ]

        if not qualified:
            return None

        # Select best opportunity by net spread
        best = max(qualified, key=lambda o: float(o.net_spread))

        self._log.info(
            "arb.signal_detected",
            market=best.market_slug,
            yes_price=str(best.yes_price),
            no_price=str(best.no_price),
            combined=str(best.combined_price),
            net_spread=str(best.net_spread),
        )

        return {"opportunity": best}

    # ─── Execute ──────────────────────────────────────────────────────────────

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """
        Execute both legs of the arb within 500ms.

        1. Compute stake from risk manager bankroll.
        2. Call risk gate.
        3. Place YES and NO legs via poly_client with asyncio.timeout.
        4. Register a single consolidated Order with direction="ARB".
        """
        opp: ArbOpportunity = signal["opportunity"]

        # Determine stake
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        stake = min(ARB_MAX_POSITION, bankroll * BET_FRACTION)

        # Each leg is stake/2 (total exposure = stake)
        leg_stake = stake / 2.0

        # Compute fees per leg
        yes_fee = POLYMARKET_CRYPTO_FEE_MULT * float(opp.yes_price) * (1.0 - float(opp.yes_price))
        no_fee = POLYMARKET_CRYPTO_FEE_MULT * float(opp.no_price) * (1.0 - float(opp.no_price))
        total_fee = (yes_fee + no_fee) * leg_stake

        # Risk gate
        approved, reason = await self._check_risk(stake)
        if not approved:
            self._log.info("arb.risk_blocked", reason=reason, market=opp.market_slug)
            return None

        # Execute both legs within 500ms timeout
        try:
            async with asyncio.timeout(ARB_MAX_EXECUTION_MS / 1000):
                yes_task = asyncio.create_task(
                    self._poly.place_order(
                        market_slug=opp.market_slug,
                        direction="YES",
                        price=opp.yes_price,
                        stake_usd=leg_stake,
                    )
                )
                no_task = asyncio.create_task(
                    self._poly.place_order(
                        market_slug=opp.market_slug,
                        direction="NO",
                        price=opp.no_price,
                        stake_usd=leg_stake,
                    )
                )
                yes_order_id, no_order_id = await asyncio.gather(yes_task, no_task)

        except asyncio.TimeoutError:
            self._log.error(
                "arb.execution_timeout",
                market=opp.market_slug,
                timeout_ms=ARB_MAX_EXECUTION_MS,
            )
            return None
        except Exception as exc:
            self._log.error("arb.execution_failed", market=opp.market_slug, error=str(exc))
            return None

        # Build consolidated ARB order
        combined = float(opp.yes_price + opp.no_price) / 2
        btc_price = float(state.btc_price) if state.btc_price else None
        order = Order(
            order_id=f"arb-{uuid.uuid4().hex[:12]}",
            strategy=self.name,
            venue="polymarket",
            direction="ARB",
            price=str(combined),
            stake_usd=stake,
            fee_usd=total_fee,
            status=OrderStatus.OPEN,
            btc_entry_price=btc_price,
            market_id=opp.market_slug,
            metadata={
                "yes_order_id": yes_order_id,
                "no_order_id": no_order_id,
                "yes_price": str(opp.yes_price),
                "no_price": str(opp.no_price),
                "combined_price": str(opp.combined_price),
                "net_spread": str(opp.net_spread),
                "net_spread_usd": float(opp.net_spread) * stake,
            },
        )

        await self._om.register_order(order)

        self._log.info(
            "arb.executed",
            order_id=order.order_id,
            market=opp.market_slug,
            yes_order_id=yes_order_id,
            no_order_id=no_order_id,
            stake=stake,
            net_spread=str(opp.net_spread),
        )

        return order
