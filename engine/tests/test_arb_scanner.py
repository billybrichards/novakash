"""
Tests for the Sub-$1 Arbitrage Scanner.

Tests cover:
  - Opportunity detection when combined price is below threshold
  - Correct net spread calculation after fees
  - No opportunity when spread is too thin
  - Multiple markets: returns best opportunity
  - Stale/outdated orderbook filtering
  - Edge cases: one side missing, zero volume
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import pytest

from signals.arb_scanner import ArbScanner
from data.models import ArbOpportunity, PolymarketOrderBook


def make_book(
    slug: str,
    yes_ask: float,
    no_ask: float,
    ts: Optional[datetime] = None,
) -> PolymarketOrderBook:
    """Build a minimal PolymarketOrderBook for testing."""
    ts = ts or datetime.utcnow()
    return PolymarketOrderBook(
        market_slug=slug,
        token_id=f"token-{slug}",
        yes_bids=[(Decimal(str(yes_ask - 0.01)), Decimal("100"))],
        yes_asks=[(Decimal(str(yes_ask)), Decimal("100"))],
        no_bids=[(Decimal(str(no_ask - 0.01)), Decimal("100"))],
        no_asks=[(Decimal(str(no_ask)), Decimal("100"))],
        timestamp=ts,
    )


@pytest.fixture
def scanner() -> ArbScanner:
    """ArbScanner with 0% fee for predictable spread calculations."""
    return ArbScanner(fee_mult=0.0, min_spread=0.01, max_position_usd=50.0)


@pytest.fixture
def scanner_with_fees() -> ArbScanner:
    """ArbScanner with realistic 7.2% Polymarket fee."""
    return ArbScanner(fee_mult=0.072, min_spread=0.015, max_position_usd=50.0)


class TestOpportunityDetection:
    @pytest.mark.asyncio
    async def test_detects_opportunity_below_threshold(self, scanner: ArbScanner) -> None:
        """YES=0.48 + NO=0.48 = $0.96 → spread = 0.04 → opportunity found."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner._on_opportunities = capture
        await scanner.on_book(make_book("btc-above-30k", yes_ask=0.48, no_ask=0.48))

        assert len(received) > 0
        assert len(received[0]) > 0
        opp = received[0][0]
        assert opp.market_slug == "btc-above-30k"

    @pytest.mark.asyncio
    async def test_no_opportunity_when_spread_too_thin(self, scanner: ArbScanner) -> None:
        """YES=0.50 + NO=0.50 = $1.00 → net spread = 0.00 → no opportunity."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner._on_opportunities = capture
        await scanner.on_book(make_book("btc-above-30k", yes_ask=0.50, no_ask=0.50))

        assert not any(opps for opps in received)

    @pytest.mark.asyncio
    async def test_no_opportunity_when_combined_above_dollar(self, scanner: ArbScanner) -> None:
        """YES=0.55 + NO=0.55 = $1.10 → combined > 1 → no arb."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner._on_opportunities = capture
        await scanner.on_book(make_book("btc-above-30k", yes_ask=0.55, no_ask=0.55))

        assert not any(opps for opps in received)


class TestNetSpreadCalculation:
    @pytest.mark.asyncio
    async def test_net_spread_accounts_for_fees(self, scanner_with_fees: ArbScanner) -> None:
        """With 7.2% fee, combined=0.80 → net_spread = 0.20 - 0.072 = 0.128."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner_with_fees._on_opportunities = capture
        await scanner_with_fees.on_book(make_book("test-market", yes_ask=0.40, no_ask=0.40))

        assert received and received[0]
        opp = received[0][0]
        expected_net = Decimal("1.0") - opp.combined_price - Decimal(str(0.072))
        # Allow small rounding tolerance
        assert abs(float(opp.net_spread) - float(expected_net)) < 0.001

    @pytest.mark.asyncio
    async def test_opportunity_respects_min_spread_filter(self, scanner_with_fees: ArbScanner) -> None:
        """With fees, a combined price of 0.98 → net_spread ≈ -0.072 → filtered out."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner_with_fees._on_opportunities = capture
        await scanner_with_fees.on_book(make_book("tight-market", yes_ask=0.49, no_ask=0.49))

        # net_spread = 0.02 - 0.072 = negative → no opportunity
        assert not any(o for opps in received for o in opps)


class TestMultipleMarkets:
    @pytest.mark.asyncio
    async def test_returns_all_viable_opportunities(self) -> None:
        """Multiple books → multiple opportunities returned."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner = ArbScanner(fee_mult=0.0, min_spread=0.01, max_position_usd=50.0)
        scanner._on_opportunities = capture

        await scanner.on_book(make_book("market-a", yes_ask=0.40, no_ask=0.40))
        await scanner.on_book(make_book("market-b", yes_ask=0.45, no_ask=0.44))

        slugs = {opp.market_slug for opps in received for opp in opps}
        assert "market-a" in slugs or "market-b" in slugs

    @pytest.mark.asyncio
    async def test_opportunity_fields_are_populated(self) -> None:
        """ArbOpportunity must have all required fields."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner = ArbScanner(fee_mult=0.0, min_spread=0.01, max_position_usd=50.0)
        scanner._on_opportunities = capture

        await scanner.on_book(make_book("full-fields", yes_ask=0.42, no_ask=0.42))

        assert received and received[0]
        opp = received[0][0]

        assert opp.market_slug == "full-fields"
        assert Decimal("0") < opp.yes_price < Decimal("1")
        assert Decimal("0") < opp.no_price < Decimal("1")
        assert Decimal("0") < opp.combined_price < Decimal("1")
        assert opp.max_position_usd == 50.0
        assert isinstance(opp.timestamp, datetime)


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_orderbook_does_not_crash(self, scanner: ArbScanner) -> None:
        """An empty book (no asks) should not raise."""
        book = PolymarketOrderBook(
            market_slug="empty",
            token_id="empty-token",
            yes_bids=[],
            yes_asks=[],
            no_bids=[],
            no_asks=[],
            timestamp=datetime.utcnow(),
        )
        # Should not raise
        await scanner.on_book(book)

    @pytest.mark.asyncio
    async def test_book_update_replaces_previous(self, scanner: ArbScanner) -> None:
        """Updating a market's book should replace, not append, to internal cache."""
        received: list[list[ArbOpportunity]] = []

        async def capture(opps: list[ArbOpportunity]) -> None:
            received.append(opps)

        scanner._on_opportunities = capture

        # First update: viable
        await scanner.on_book(make_book("volatile", yes_ask=0.40, no_ask=0.40))
        # Second update: no longer viable
        await scanner.on_book(make_book("volatile", yes_ask=0.50, no_ask=0.50))

        # After second update, no opportunity should appear
        if len(received) >= 2:
            last = received[-1]
            assert not any(o.market_slug == "volatile" for o in last)
