"""
Tests for ArbScanner.

Tests opportunity detection, fee calculation, thin quote filtering,
and callback invocation.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from decimal import Decimal
from typing import List

from data.models import ArbOpportunity, PolymarketOrderBook
from signals.arb_scanner import ArbScanner, _MIN_QUOTE_SIZE_USD


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_book(
    market_slug: str,
    yes_asks: list[tuple[str, str]] | None = None,
    no_asks: list[tuple[str, str]] | None = None,
    yes_bids: list[tuple[str, str]] | None = None,
    no_bids: list[tuple[str, str]] | None = None,
) -> PolymarketOrderBook:
    """Create a PolymarketOrderBook with the given ask/bid levels."""

    def _parse(levels: list[tuple[str, str]] | None) -> list[tuple[Decimal, Decimal]]:
        if not levels:
            return []
        return [(Decimal(p), Decimal(s)) for p, s in levels]

    return PolymarketOrderBook(
        market_slug=market_slug,
        token_id=f"token-{market_slug}",
        yes_asks=_parse(yes_asks),
        yes_bids=_parse(yes_bids),
        no_asks=_parse(no_asks),
        no_bids=_parse(no_bids),
        timestamp=datetime.utcnow(),
    )


# ─── Fee Calculation ──────────────────────────────────────────────────────────

def test_fee_formula_matches_spec():
    """Fee per leg = fee_mult * price * (1 - price)."""
    scanner = ArbScanner(fee_mult=0.072)

    price = 0.45
    expected = 0.072 * price * (1 - price)
    actual = scanner._fee(price)
    assert abs(actual - expected) < 1e-10, f"Fee mismatch: {actual} != {expected}"


def test_fee_at_fifty_percent():
    """Fee is maximised at price=0.5 for symmetric market."""
    scanner = ArbScanner(fee_mult=0.072)
    fee_50 = scanner._fee(0.5)

    # At 0.5: fee = 0.072 * 0.5 * 0.5 = 0.018
    assert abs(fee_50 - 0.018) < 1e-10


def test_fee_at_extremes():
    """Fee approaches 0 at price=0 and price=1."""
    scanner = ArbScanner(fee_mult=0.072)
    assert scanner._fee(0.0) == 0.0
    assert scanner._fee(1.0) == 0.0


def test_fee_is_symmetric():
    """fee(p) == fee(1-p) due to symmetry of price * (1-price)."""
    scanner = ArbScanner(fee_mult=0.072)
    assert abs(scanner._fee(0.3) - scanner._fee(0.7)) < 1e-12


# ─── Thin Quote Filtering ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_thin_quote_filtered():
    """Books with ask size < $10 should be ignored (no opportunity detected)."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    # YES: price=0.4, size=5 (thin — below $10)
    yes_book = _make_book(
        "btc-test",
        yes_asks=[("0.4", "5.0")],  # size=5 → too thin
    )
    # NO: price=0.4, size=20 (ok)
    no_book = _make_book(
        "btc-test",
        no_asks=[("0.4", "20.0")],
    )

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    # No opportunity because YES side is thin
    assert len(opps) == 0


@pytest.mark.asyncio
async def test_thin_no_side_filtered():
    """If the NO side is thin, opportunity should be filtered out."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    yes_book = _make_book("btc-test2", yes_asks=[("0.4", "50.0")])
    no_book = _make_book("btc-test2", no_asks=[("0.4", "5.0")])  # thin

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    assert len(opps) == 0


# ─── Opportunity Detection ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_opportunity_detected_below_985():
    """
    Should detect opportunity when combined price is significantly below $1.

    With yes=0.47, no=0.47, combined=0.94, fees < 0.015 → net_spread > 0.
    """
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    yes_book = _make_book("btc-arb", yes_asks=[("0.47", "100.0")])
    no_book = _make_book("btc-arb", no_asks=[("0.47", "100.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    assert len(opps) == 1
    opp = opps[0][0]
    assert float(opp.net_spread) > 0
    assert float(opp.combined_price) < 1.0


@pytest.mark.asyncio
async def test_no_opportunity_above_net_zero():
    """Should not detect opportunity when combined + fees >= 1.0."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    # combined = 1.0 → no profit after fees
    yes_book = _make_book("btc-no-arb", yes_asks=[("0.50", "100.0")])
    no_book = _make_book("btc-no-arb", no_asks=[("0.50", "100.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    # net_spread = 1 - 0.5 - 0.5 - fee_yes - fee_no = 0 - fees < 0
    assert len(opps) == 0


@pytest.mark.asyncio
async def test_opportunity_fields_correct():
    """ArbOpportunity should have all required fields populated."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    yes_book = _make_book("btc-fields", yes_asks=[("0.45", "50.0")])
    no_book = _make_book("btc-fields", no_asks=[("0.45", "50.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    assert len(opps) == 1
    opp = opps[0][0]

    # Verify all fields
    assert opp.market_slug == "btc-fields"
    assert float(opp.yes_price) == pytest.approx(0.45, abs=1e-6)
    assert float(opp.no_price) == pytest.approx(0.45, abs=1e-6)
    assert float(opp.combined_price) == pytest.approx(0.90, abs=1e-6)
    assert float(opp.net_spread) > 0
    assert opp.max_position_usd == pytest.approx(50.0, abs=1e-6)
    assert opp.timestamp is not None


@pytest.mark.asyncio
async def test_net_spread_formula():
    """net_spread = 1 - yes - no - fee_yes - fee_no."""
    scanner = ArbScanner(fee_mult=0.072)

    yes_price = 0.45
    no_price = 0.45
    fee_yes = 0.072 * yes_price * (1 - yes_price)
    fee_no = 0.072 * no_price * (1 - no_price)
    expected_spread = 1.0 - yes_price - no_price - fee_yes - fee_no

    yes_book = _make_book("btc-net", yes_asks=[(str(yes_price), "50.0")])
    no_book = _make_book("btc-net", no_asks=[(str(no_price), "50.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    opps = scanner.get_all_opportunities()
    assert len(opps) == 1
    assert float(opps[0].net_spread) == pytest.approx(expected_spread, abs=1e-6)


@pytest.mark.asyncio
async def test_max_position_limited_by_thinner_leg():
    """max_position_usd should be min(yes_size, no_size)."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    yes_book = _make_book("btc-pos", yes_asks=[("0.45", "30.0")])
    no_book = _make_book("btc-pos", no_asks=[("0.45", "80.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    assert len(opps) == 1
    assert opps[0][0].max_position_usd == pytest.approx(30.0)  # min(30, 80)


@pytest.mark.asyncio
async def test_get_all_opportunities_sorted():
    """get_all_opportunities should return sorted by net_spread descending."""
    scanner = ArbScanner(fee_mult=0.072)

    # Feed two different markets
    for slug, yes_p, no_p in [
        ("market-a", "0.42", "0.42"),   # bigger spread
        ("market-b", "0.48", "0.48"),   # smaller spread
    ]:
        yes_book = _make_book(slug, yes_asks=[(yes_p, "100.0")])
        no_book = _make_book(slug, no_asks=[(no_p, "100.0")])
        await scanner.on_book(yes_book, "YES")
        await scanner.on_book(no_book, "NO")

    all_opps = scanner.get_all_opportunities()
    # Should be sorted descending
    if len(all_opps) >= 2:
        assert float(all_opps[0].net_spread) >= float(all_opps[1].net_spread)


@pytest.mark.asyncio
async def test_empty_book_no_opportunity():
    """Empty ask lists should produce no opportunities."""
    opps: list[list[ArbOpportunity]] = []

    async def capture(o):
        opps.append(o)

    scanner = ArbScanner(fee_mult=0.072, on_opportunities=capture)

    yes_book = _make_book("empty-market", yes_asks=[])
    no_book = _make_book("empty-market", no_asks=[("0.45", "100.0")])

    await scanner.on_book(yes_book, "YES")
    await scanner.on_book(no_book, "NO")

    assert len(opps) == 0
