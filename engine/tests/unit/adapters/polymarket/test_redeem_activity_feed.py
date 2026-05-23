"""Regression tests for PolymarketRedemptionFeed.

Pinned against the data-api activity schema observed on 2026-05-23 from
funder 0x181d...5E10. Each REDEEM row looks like:

    {
      "proxyWallet": "0x181d...",
      "timestamp": 1779543638,
      "conditionId": "0x0771...",
      "type": "REDEEM",
      "size": 15,
      "usdcSize": 15,
      "transactionHash": "0xcb1e...",
      "slug": "btc-updown-5m-1779543300",
      "eventSlug": "btc-updown-5m-1779543300",
      ...
    }
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from adapters.polymarket.redeem_activity_feed import (
    PolymarketRedemptionFeed,
    clear_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


SAMPLE_ROW = {
    "proxyWallet": "0x181d2ed714e0f7fe9c6e4f13711376edaab25e10",
    "timestamp": 1779543638,
    "conditionId": (
        "0x0771fde4fbe150953cc76605f371722fbe8cebd5e14a0b23419f16485fcb4a7a"
    ),
    "type": "REDEEM",
    "size": 15,
    "usdcSize": 15,
    "transactionHash": (
        "0xcb1ed7b5e18637e29fb92c460107031d69c9fe6b459df187e8206f7d9cd7878d"
    ),
    "price": 0,
    "asset": "",
    "side": "",
    "outcomeIndex": 999,
    "title": "Bitcoin Up or Down - May 23, 9:35AM-9:40AM ET",
    "slug": "btc-updown-5m-1779543300",
    "eventSlug": "btc-updown-5m-1779543300",
    "outcome": "",
}


@pytest.mark.asyncio
async def test_parses_well_formed_row():
    feed = PolymarketRedemptionFeed(
        "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
    )

    async def fake_fetch(*, limit, offset=0):
        return [SAMPLE_ROW]

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        events = await feed.fetch_recent()

    assert len(events) == 1
    e = events[0]
    assert e.market_slug == "btc-updown-5m-1779543300"
    assert (
        e.transaction_hash
        == "0xcb1ed7b5e18637e29fb92c460107031d69c9fe6b459df187e8206f7d9cd7878d"
    )
    assert e.condition_id.startswith("0x0771fde4")
    assert e.usdc_size == 15.0
    assert e.size == 15.0
    assert e.timestamp == 1779543638
    assert e.funder_address == "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"


@pytest.mark.asyncio
async def test_skips_rows_with_missing_required_fields():
    feed = PolymarketRedemptionFeed("0x181D")

    bad_no_tx = dict(SAMPLE_ROW)
    bad_no_tx["transactionHash"] = ""

    bad_no_slug = dict(SAMPLE_ROW)
    bad_no_slug["slug"] = ""
    bad_no_slug["eventSlug"] = ""

    async def fake_fetch(*, limit, offset=0):
        return [bad_no_tx, bad_no_slug, SAMPLE_ROW]

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        events = await feed.fetch_recent()

    assert len(events) == 1
    assert events[0].transaction_hash.startswith("0xcb1e")


@pytest.mark.asyncio
async def test_empty_when_fetch_returns_nothing():
    feed = PolymarketRedemptionFeed("0x181D")

    async def fake_fetch(*, limit, offset=0):
        return []

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        events = await feed.fetch_recent()
    assert events == []


@pytest.mark.asyncio
async def test_cache_amortises_repeated_calls():
    """Two calls within cache_ttl_s share one HTTP fetch."""
    feed = PolymarketRedemptionFeed("0x181D", cache_ttl_s=10.0)

    call_count = {"n": 0}

    async def fake_fetch(*, limit, offset=0):
        call_count["n"] += 1
        return [SAMPLE_ROW]

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        await feed.fetch_recent()
        await feed.fetch_recent()
        await feed.fetch_recent()
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_cache_bypass_when_use_cache_false():
    feed = PolymarketRedemptionFeed("0x181D", cache_ttl_s=10.0)

    call_count = {"n": 0}

    async def fake_fetch(*, limit, offset=0):
        call_count["n"] += 1
        return [SAMPLE_ROW]

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        await feed.fetch_recent(use_cache=False)
        await feed.fetch_recent(use_cache=False)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_fetch_since_dedupes_by_tx_hash_across_pages():
    feed = PolymarketRedemptionFeed("0x181D")

    page_n = {"n": 0}

    async def fake_fetch(*, limit, offset=0):
        page_n["n"] += 1
        if page_n["n"] == 1:
            # Page 1: a SAMPLE row at ts=1779543638 (newer)
            return [SAMPLE_ROW]
        if page_n["n"] == 2:
            # Page 2: same row appears (server overlap)
            return [SAMPLE_ROW]
        return []

    with patch.object(feed, "_fetch_async", side_effect=fake_fetch):
        events = await feed.fetch_since(
            since_timestamp=1779000000, page_size=1, max_pages=3
        )
    # Dedup by tx_hash
    assert len(events) == 1


@pytest.mark.asyncio
async def test_funder_address_required():
    with pytest.raises(ValueError):
        PolymarketRedemptionFeed("")
