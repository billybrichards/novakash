"""
Sub-$1 Arbitrage Scanner

Scans Polymarket BTC range markets for YES+NO mispricing.

In a binary prediction market, YES + NO should theoretically sum to $1.00
(plus fees). When the combined best-ask is below $1.00 - fees, buying
both sides guarantees a profit regardless of outcome.

Net spread formula:
  combined_price = best_ask(YES) + best_ask(NO)
  net_spread = (1.0 - combined_price) - (fee_rate * 1.0)

A trade is viable when:
  net_spread >= ARB_MIN_SPREAD (0.015, i.e. 1.5 cents)

Fee note:
  Polymarket charges POLYMARKET_CRYPTO_FEE_MULT (7.2%) on crypto markets.
  This eats heavily into arb spread — filter must account for this.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable
import structlog

from config.constants import (
    POLYMARKET_CRYPTO_FEE_MULT,
    ARB_MIN_SPREAD,
    ARB_MAX_POSITION,
    POLY_WINDOW_SECONDS,
)
from data.models import PolymarketOrderBook, ArbOpportunity

log = structlog.get_logger(__name__)


class ArbScanner:
    """
    Maintains a cache of order books for all tracked markets
    and emits ArbOpportunity objects when viable spreads are found.

    Call on_book() with each new PolymarketOrderBook snapshot.
    Opportunities are re-evaluated on every book update.
    """

    def __init__(
        self,
        fee_mult: float = POLYMARKET_CRYPTO_FEE_MULT,
        min_spread: float = ARB_MIN_SPREAD,
        max_position_usd: float = ARB_MAX_POSITION,
        on_opportunities: Callable[[list[ArbOpportunity]], Awaitable[None]] | None = None,
    ) -> None:
        self.fee_mult = fee_mult
        self.min_spread = min_spread
        self.max_position_usd = max_position_usd
        self._on_opportunities = on_opportunities

        # market_slug → {"yes": PolymarketOrderBook, "no": PolymarketOrderBook}
        self._books: dict[str, dict[str, PolymarketOrderBook]] = {}

    async def on_book(self, book: PolymarketOrderBook, side: str = "yes") -> None:
        """
        Ingest a new order book snapshot.

        Args:
            book: The order book snapshot from Polymarket.
            side: "yes" or "no" — which token this book belongs to.
        """
        slug = book.market_slug
        if slug not in self._books:
            self._books[slug] = {}
        self._books[slug][side] = book

        # Only scan when we have both YES and NO books
        if "yes" in self._books[slug] and "no" in self._books[slug]:
            await self._scan_market(slug)

    async def _scan_market(self, market_slug: str) -> None:
        """Check a specific market for arb opportunities."""
        yes_book = self._books[market_slug]["yes"]
        no_book = self._books[market_slug]["no"]

        best_yes_ask = self._best_ask(yes_book.yes_asks)
        best_no_ask = self._best_ask(no_book.no_asks)

        if best_yes_ask is None or best_no_ask is None:
            return

        combined = best_yes_ask + best_no_ask
        # Net spread: what's left after buying both sides and paying fees
        total_fee = Decimal(str(self.fee_mult)) * combined
        net_spread = Decimal("1.0") - combined - total_fee

        if net_spread >= Decimal(str(self.min_spread)):
            opp = ArbOpportunity(
                market_slug=market_slug,
                yes_price=best_yes_ask,
                no_price=best_no_ask,
                combined_price=combined,
                net_spread=net_spread,
                max_position_usd=self.max_position_usd,
                timestamp=datetime.utcnow(),
            )
            log.info(
                "arb.opportunity_found",
                market=market_slug,
                combined=str(combined),
                net_spread=str(net_spread),
            )
            if self._on_opportunities:
                await self._on_opportunities([opp])

    def _best_ask(self, asks: list[tuple[Decimal, Decimal]]) -> Decimal | None:
        """Return the lowest ask price from an order book level list."""
        if not asks:
            return None
        return min(price for price, _ in asks)

    def get_all_opportunities(self) -> list[ArbOpportunity]:
        """
        Re-scan all cached books and return current opportunities.
        Used for backtest / paper trading replay.
        """
        opps = []
        for slug, sides in self._books.items():
            if "yes" not in sides or "no" not in sides:
                continue
            yes_book = sides["yes"]
            no_book = sides["no"]
            best_yes = self._best_ask(yes_book.yes_asks)
            best_no = self._best_ask(no_book.no_asks)
            if best_yes is None or best_no is None:
                continue
            combined = best_yes + best_no
            net_spread = Decimal("1.0") - combined - Decimal(str(self.fee_mult)) * combined
            if net_spread >= Decimal(str(self.min_spread)):
                opps.append(ArbOpportunity(
                    market_slug=slug,
                    yes_price=best_yes,
                    no_price=best_no,
                    combined_price=combined,
                    net_spread=net_spread,
                    max_position_usd=self.max_position_usd,
                    timestamp=datetime.utcnow(),
                ))
        return opps
