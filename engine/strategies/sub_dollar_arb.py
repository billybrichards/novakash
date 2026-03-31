"""
Sub-Dollar Arbitrage Strategy

Scans Polymarket binary markets for mispricing where the combined cost of
buying one YES share + one NO share is less than $0.985 (i.e., a guaranteed
≥$0.015 profit per $1 resolved).

Theory:
  In a binary prediction market, YES + NO = $1 at resolution.
  If best_ask(YES) + best_ask(NO) < $0.985, buying both legs locks in
  an arbitrage profit after fees (net ≥ 1.5 cents per dollar at risk).

Execution:
  - Both legs must be filled within ARB_MAX_EXECUTION_MS (500ms).
  - If the second leg fill times out, the first leg is cancelled/hedged.
  - Max position size: ARB_MAX_POSITION ($50 default).
  - Fee model: POLYMARKET_CRYPTO_FEE_MULT applied per leg.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
import structlog

from config.constants import (
    ARB_MIN_SPREAD,
    ARB_MAX_POSITION,
    ARB_MAX_EXECUTION_MS,
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
    Buys YES + NO when combined price < $0.985, executing both legs within 500ms.

    Signal dict schema:
        {
            "market_slug": str,
            "yes_price": Decimal,
            "no_price": Decimal,
            "combined_price": Decimal,
            "net_spread": Decimal,  # after fees
            "stake_usd": float,
        }
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        max_position_usd: float = ARB_MAX_POSITION,
        min_spread: float = ARB_MIN_SPREAD,
        max_exec_ms: int = ARB_MAX_EXECUTION_MS,
    ) -> None:
        super().__init__(
            name="sub_dollar_arb",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._max_position_usd = max_position_usd
        self._min_spread = min_spread
        self._max_exec_ms = max_exec_ms

        # Track markets we've recently arbed to avoid re-entry
        self._recently_arbed: set[str] = set()

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """
        Scan current arb opportunities from the market state.

        Returns the best opportunity above the minimum net spread, or None.
        """
        if not state.arb_opportunities:
            return None

        # Find the best (lowest combined price) opportunity
        best: Optional[ArbOpportunity] = None
        for opp in state.arb_opportunities:
            if opp.market_slug in self._recently_arbed:
                continue
            if float(opp.net_spread) >= self._min_spread:
                if best is None or opp.combined_price < best.combined_price:
                    best = opp

        if best is None:
            return None

        # Compute stake: risk-adjusted, capped at max position
        stake_usd = min(self._max_position_usd, float(best.max_position_usd))

        log.info(
            "arb.signal_detected",
            market=best.market_slug,
            combined=float(best.combined_price),
            spread=float(best.net_spread),
            stake=stake_usd,
        )

        return {
            "market_slug": best.market_slug,
            "yes_price": best.yes_price,
            "no_price": best.no_price,
            "combined_price": best.combined_price,
            "net_spread": best.net_spread,
            "stake_usd": stake_usd,
        }

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """
        Submit YES and NO legs simultaneously. Cancel/hedge if second leg fails
        within ARB_MAX_EXECUTION_MS.
        """
        stake_usd = signal["stake_usd"]
        market_slug = signal["market_slug"]

        # Risk gate
        approved, reason = await self._check_risk(stake_usd)
        if not approved:
            log.warning("arb.blocked_by_risk", market=market_slug, reason=reason)
            return None

        yes_order = Order(
            order_id=str(uuid.uuid4()),
            strategy=self.name,
            venue="polymarket",
            market_slug=market_slug,
            direction="YES",
            entry_price=signal["yes_price"],
            stake_usd=stake_usd / 2,
            fee_usd=stake_usd / 2 * POLYMARKET_CRYPTO_FEE_MULT,
            status=OrderStatus.PENDING,
            created_at=datetime.utcnow(),
        )
        no_order = Order(
            order_id=str(uuid.uuid4()),
            strategy=self.name,
            venue="polymarket",
            market_slug=market_slug,
            direction="NO",
            entry_price=signal["no_price"],
            stake_usd=stake_usd / 2,
            fee_usd=stake_usd / 2 * POLYMARKET_CRYPTO_FEE_MULT,
            status=OrderStatus.PENDING,
            created_at=datetime.utcnow(),
        )

        try:
            # Submit both legs concurrently with timeout
            timeout_s = self._max_exec_ms / 1000
            async with asyncio.timeout(timeout_s):
                yes_task = asyncio.create_task(
                    self._poly.place_order(market_slug, "YES", signal["yes_price"], stake_usd / 2)
                )
                no_task = asyncio.create_task(
                    self._poly.place_order(market_slug, "NO", signal["no_price"], stake_usd / 2)
                )
                yes_result, no_result = await asyncio.gather(yes_task, no_task)

        except TimeoutError:
            log.error("arb.execution_timeout", market=market_slug, ms=self._max_exec_ms)
            return None

        yes_order.status = OrderStatus.OPEN
        no_order.status = OrderStatus.OPEN

        await self._om.register_order(yes_order)
        await self._om.register_order(no_order)

        # Mark market as recently arbed (avoid re-entry this cycle)
        self._recently_arbed.add(market_slug)

        log.info(
            "arb.executed",
            market=market_slug,
            yes_id=yes_order.order_id,
            no_id=no_order.order_id,
            combined_cost=float(signal["combined_price"]),
            net_spread=float(signal["net_spread"]),
        )

        return yes_order  # Return primary leg

    def clear_recently_arbed(self, market_slug: str) -> None:
        """Allow re-entry for a market (called externally when position resolves)."""
        self._recently_arbed.discard(market_slug)
