"""
Arb Scanner — detects cross-market arbitrage opportunities on Polymarket.

Fee model (per leg):
    fee_leg = fee_mult * price * (1 - price)

Total arb cost = fee_yes + fee_no
Net spread = 1.0 - yes_price - no_price - fee_yes - fee_no

Only quotes with size >= $10 are considered (thin/stale quotes are filtered).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Awaitable, Optional

import structlog

from data.models import ArbOpportunity, PolymarketOrderBook

log = structlog.get_logger(__name__)

# Minimum quote size to be considered liquid
_MIN_QUOTE_SIZE_USD: float = 10.0


class ArbScanner:
    """
    Scans Polymarket order books for sub-$1 arbitrage opportunities.

    Detects markets where combined YES ask + NO ask price < $1.00 after fees,
    guaranteeing a risk-free profit on resolution.

    Parameters
    ----------
    fee_mult:
        Polymarket fee multiplier used in the per-leg fee formula.
        Defaults to 0.072 (7.2% for crypto markets).
    on_opportunities:
        Optional async callback invoked with a ``list[ArbOpportunity]``
        whenever the book is updated and opportunities exist.
    """

    def __init__(
        self,
        fee_mult: float = 0.072,
        on_opportunities: Optional[
            Callable[[list[ArbOpportunity]], Awaitable[None]]
        ] = None,
    ) -> None:
        self._fee_mult = fee_mult
        self._on_opportunities = on_opportunities

        # Latest books keyed by market_slug → {side: PolymarketOrderBook}
        self._books: dict[str, dict[str, PolymarketOrderBook]] = {}

        # Latest computed opportunities
        self._opportunities: list[ArbOpportunity] = []

        self._log = log.bind(component="ArbScanner", fee_mult=fee_mult)
        self._log.info("initialised")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def on_book(self, book: PolymarketOrderBook, side: str) -> None:
        """
        Ingest an updated order book snapshot.

        Parameters
        ----------
        book:
            Updated :class:`PolymarketOrderBook`.
        side:
            ``"YES"`` or ``"NO"`` — which side of the market this book represents.
        """
        market_slug = book.market_slug
        if market_slug not in self._books:
            self._books[market_slug] = {}
        self._books[market_slug][side.upper()] = book

        self._log.debug(
            "book_updated",
            market_slug=market_slug,
            side=side,
            sides_available=list(self._books[market_slug].keys()),
        )

        # Only scan when both sides are available
        if "YES" in self._books[market_slug] and "NO" in self._books[market_slug]:
            opportunities = self._scan_market(market_slug)
            if opportunities:
                self._opportunities = opportunities
                self._log.info(
                    "arb_opportunities_found",
                    market_slug=market_slug,
                    count=len(opportunities),
                    best_spread=str(max(o.net_spread for o in opportunities)),
                )
                if self._on_opportunities is not None:
                    try:
                        await self._on_opportunities(opportunities)
                    except Exception:
                        self._log.exception("on_opportunities callback raised")

    def get_all_opportunities(self) -> list[ArbOpportunity]:
        """
        Return all currently known arb opportunities.

        Returns
        -------
        list[ArbOpportunity]:
            Sorted by net_spread descending.
        """
        return sorted(self._opportunities, key=lambda o: float(o.net_spread), reverse=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fee(self, price: float) -> float:
        """Compute the fee for one leg at the given price."""
        return self._fee_mult * price * (1.0 - price)

    def _best_ask(
        self, book: PolymarketOrderBook, side: str
    ) -> Optional[tuple[float, float]]:
        """
        Return ``(price, size_usd)`` for the best ask on the given side,
        or ``None`` if the best ask has size < $10 (thin/stale).

        For YES side: uses yes_asks.
        For NO side:  uses no_asks.
        """
        asks = book.yes_asks if side == "YES" else book.no_asks
        if not asks:
            return None

        # asks are sorted ascending by price; lowest ask = best ask
        best_price, best_size = asks[0]
        price_f = float(best_price)
        size_f = float(best_size)

        if size_f < _MIN_QUOTE_SIZE_USD:
            self._log.debug(
                "thin_quote_filtered",
                market_slug=book.market_slug,
                side=side,
                price=price_f,
                size_usd=size_f,
            )
            return None
        return price_f, size_f

    def _scan_market(self, market_slug: str) -> list[ArbOpportunity]:
        """Scan a single market for arb given its YES and NO books."""
        yes_book = self._books[market_slug]["YES"]
        no_book = self._books[market_slug]["NO"]

        yes_ask = self._best_ask(yes_book, "YES")
        no_ask = self._best_ask(no_book, "NO")

        if yes_ask is None or no_ask is None:
            return []

        yes_price, yes_size = yes_ask
        no_price, no_size = no_ask

        fee_yes = self._fee(yes_price)
        fee_no = self._fee(no_price)
        combined = yes_price + no_price
        net_spread = 1.0 - combined - fee_yes - fee_no

        self._log.debug(
            "arb_scan",
            market_slug=market_slug,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            combined=round(combined, 4),
            fee_yes=round(fee_yes, 6),
            fee_no=round(fee_no, 6),
            net_spread=round(net_spread, 6),
        )

        if net_spread <= 0:
            return []

        # Maximum position is limited by liquidity on the thinner leg
        max_pos = min(yes_size, no_size)

        return [
            ArbOpportunity(
                market_slug=market_slug,
                yes_price=Decimal(str(round(yes_price, 6))),
                no_price=Decimal(str(round(no_price, 6))),
                combined_price=Decimal(str(round(combined, 6))),
                net_spread=Decimal(str(round(net_spread, 6))),
                max_position_usd=max_pos,
                timestamp=datetime.now(tz=timezone.utc),
            )
        ]
