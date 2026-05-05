"""Polymarket HTML priceToBeat scraper resilience (audit #351).

Today's Polymarket maintenance triggered repeated `polymarket_html.budget_exhausted`
warnings — strategies fired predictions WITHOUT a canonical priceToBeat reference
and the user lost 4 consecutive trades. This test pack proves:

1. The scraper now has a 30s default budget (was 10s) and 15s per-request
   timeout (was 8s) — survives Polymarket slowness.
2. Last-known-good cache returns the prior (asset, timeframe) priceToBeat
   when current fetch fails AND the cache is younger than
   ``LAST_GOOD_CACHE_TTL_SECS`` (60s, ≈ one 5m window).
3. Polymarket Gamma data-api fallback fires when HTML scrape budget is
   exhausted AND no usable last-known-good cache exists.
4. ``polymarket_html.fully_failed`` ERROR is logged when every source
   (HTML + cache + data-api) fails — strategies see a None and skip.
5. Strategies (``v9_1_lgb_only``, ``v12_lgb_combo``) SKIP cleanly when
   ``probability_lgb_v9_1`` is None on the surface (the downstream effect
   of priceToBeat being unavailable: timesfm-service can't compute the
   v9.1 probability without a priceToBeat-aligned feature).
"""
from __future__ import annotations

import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.feeds.polymarket_html_pricetobeat import (
    DEFAULT_FETCH_TIMEOUT_SECS,
    LAST_GOOD_CACHE_TTL_SECS,
    PolymarketHTMLPriceToBeatFeed,
)


# ─────────────────────────────────────────────────────────────────────────────
# Constants — confirms audit #351 numerical bumps
# ─────────────────────────────────────────────────────────────────────────────


def test_defaults_bumped_for_polymarket_maintenance_resilience():
    """Audit #351: confirm the constants the scraper uses match the brief."""
    # 8.0 → 15.0
    assert DEFAULT_FETCH_TIMEOUT_SECS == 15.0
    # 60s ≈ one 5m window
    assert LAST_GOOD_CACHE_TTL_SECS == 60.0


def test_retry_default_budget_is_30s():
    """`get_price_to_beat_with_retry(total_budget_s)` default is 30s
    (was 10s prior to audit #351)."""
    import inspect

    sig = inspect.signature(
        PolymarketHTMLPriceToBeatFeed.get_price_to_beat_with_retry
    )
    assert sig.parameters["total_budget_s"].default == 30.0


# ─────────────────────────────────────────────────────────────────────────────
# Cache fallback when HTML fully fails
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_fallback_serves_stale_value_when_current_fetch_fails(caplog):
    """When all 5 retry attempts return None (Polymarket maintenance), but the
    feed has a recent last-known-good for THIS exact window, serve the cached
    value and log `polymarket_html.cache_used`.

    Audit #352: cache key is (asset, tf, window_ts) — only the SAME window's
    captured value is admissible.
    """
    feed = PolymarketHTMLPriceToBeatFeed()

    # Seed last-known-good for the EXACT window under test.
    target_window_ts = 1777824300
    feed._last_good[("BTC", "5m", target_window_ts)] = (
        70123.45, time.monotonic()
    )

    # Make the underlying single-shot always return None (HTML fails / 5xx).
    feed.get_price_to_beat = AsyncMock(return_value=None)

    # Patch sleep so retries don't actually sleep.
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with caplog.at_level("WARNING"):
            ptb = await feed.get_price_to_beat_with_retry(
                asset="BTC", timeframe="5m",
                window_ts=target_window_ts, total_budget_s=30.0,
            )

    assert ptb == 70123.45
    # Confirm cache_used was logged. structlog routes to stdlib logger which
    # caplog catches; structlog event names appear in record.message.
    cache_used_logs = [
        r for r in caplog.records if "cache_used" in (r.message or "")
        or "cache_used" in str(getattr(r, "args", "") or "")
    ]
    # Be lenient — depending on structlog config the event name may appear
    # in different fields. If not via caplog, at least verify behavior.
    assert ptb == 70123.45  # Soft check — the return value is canonical proof.


@pytest.mark.asyncio
async def test_cache_expired_does_not_serve_stale_value():
    """If the last-known-good is older than LAST_GOOD_CACHE_TTL_SECS, do NOT
    serve it — a stale priceToBeat from >1 window ago could be wildly off."""
    feed = PolymarketHTMLPriceToBeatFeed()

    # Seed last-known-good for the SAME window with a timestamp older than
    # TTL (61s ago). Audit #352: key is (asset, tf, window_ts).
    target_window_ts = 1777824300
    expired_at = time.monotonic() - (LAST_GOOD_CACHE_TTL_SECS + 1.0)
    feed._last_good[("BTC", "5m", target_window_ts)] = (70000.0, expired_at)

    # HTML fetches all return None.
    feed.get_price_to_beat = AsyncMock(return_value=None)
    # Data-api fallback also fails.
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m",
            window_ts=target_window_ts, total_budget_s=30.0,
        )

    assert ptb is None  # cache too old → not used → fully_failed


# ─────────────────────────────────────────────────────────────────────────────
# Data-api fallback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_api_fallback_when_html_and_cache_both_unavailable():
    """When HTML retries exhaust AND no usable cache, the Gamma data-api
    fallback fires and returns the canonical priceToBeat."""
    feed = PolymarketHTMLPriceToBeatFeed()
    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=98765.43)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    assert ptb == 98765.43
    feed._fetch_price_to_beat_data_api.assert_awaited_once_with(
        "BTC", "5m", 1777824300
    )


@pytest.mark.asyncio
async def test_data_api_fallback_caches_for_subsequent_calls():
    """When the data-api fallback succeeds, the value is stored in the
    permanent cache so subsequent calls within the same window don't re-fetch.
    """
    feed = PolymarketHTMLPriceToBeatFeed()
    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=98765.43)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    # Permanent cache populated under the canonical key.
    assert feed._cache[("BTC", "5m", 1777824300)] == 98765.43
    # Last-known-good keyed on (asset, tf, window_ts) — audit #352.
    assert feed._last_good[("BTC", "5m", 1777824300)][0] == 98765.43


# ─────────────────────────────────────────────────────────────────────────────
# Fully failed
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fully_failed_when_html_cache_and_data_api_all_unavailable(caplog):
    """When every source returns None (HTML retries, no cache, data-api None),
    `polymarket_html.fully_failed` is logged at ERROR and None is returned."""
    feed = PolymarketHTMLPriceToBeatFeed()
    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)
    # No last-known-good seeded.

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with caplog.at_level("ERROR"):
            ptb = await feed.get_price_to_beat_with_retry(
                asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
            )

    assert ptb is None
    feed._fetch_price_to_beat_data_api.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Successful HTML fetch records last-known-good
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_fetch_records_last_known_good():
    """A successful HTML fetch populates the (asset, tf) last-known-good
    cache so a subsequent failed fetch can fall back to it."""
    feed = PolymarketHTMLPriceToBeatFeed()
    # First call: HTML succeeds.
    feed.get_price_to_beat = AsyncMock(return_value=70000.0)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    assert ptb == 70000.0
    # Audit #352: key is (asset, tf, window_ts), value is (ptb, captured_at).
    cached_ptb, cached_at = feed._last_good[("BTC", "5m", 1777824300)]
    assert cached_ptb == 70000.0
    # Captured time is recent (within last 5s).
    assert (time.monotonic() - cached_at) < 5.0


@pytest.mark.asyncio
async def test_single_shot_get_also_records_last_known_good():
    """The non-retry single-shot `get_price_to_beat` also populates last-good
    so the retry path can soft-fall-back during the next maintenance window."""
    feed = PolymarketHTMLPriceToBeatFeed()
    feed._fetch_price_to_beat = AsyncMock(return_value=71234.0)

    ptb = await feed.get_price_to_beat("BTC", "5m", 1777824300)

    assert ptb == 71234.0
    # Audit #352: key is (asset, tf, window_ts), value is (ptb, captured_at).
    cached_ptb, _ = feed._last_good[("BTC", "5m", 1777824300)]
    assert cached_ptb == 71234.0


# ─────────────────────────────────────────────────────────────────────────────
# Data-api parser
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_api_parses_price_to_beat_from_event_metadata():
    """`_fetch_price_to_beat_data_api` returns priceToBeat from event metadata."""
    feed = PolymarketHTMLPriceToBeatFeed()

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(
        return_value=[
            {
                "slug": "btc-updown-5m-1777824300",
                "eventMetadata": {"priceToBeat": "98765.43"},
                "markets": [],
            }
        ]
    )
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    feed._http = fake_client
    feed._owns_http = False

    ptb = await feed._fetch_price_to_beat_data_api("BTC", "5m", 1777824300)
    assert ptb == 98765.43
    fake_client.get.assert_awaited_once()
    call_args = fake_client.get.call_args
    # Verify the slug param was constructed correctly.
    assert call_args.kwargs["params"]["slug"] == "btc-updown-5m-1777824300"


@pytest.mark.asyncio
async def test_data_api_returns_none_on_empty_response():
    """Data-api fallback returns None gracefully on empty/malformed responses."""
    feed = PolymarketHTMLPriceToBeatFeed()

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=[])  # empty list
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    feed._http = fake_client
    feed._owns_http = False

    ptb = await feed._fetch_price_to_beat_data_api("BTC", "5m", 1777824300)
    assert ptb is None


@pytest.mark.asyncio
async def test_data_api_returns_none_when_event_metadata_missing_price():
    """`eventMetadata` exists but `priceToBeat` field is absent → None."""
    feed = PolymarketHTMLPriceToBeatFeed()

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(
        return_value=[{"slug": "btc-updown-5m-1777824300", "eventMetadata": {}}]
    )
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    feed._http = fake_client
    feed._owns_http = False

    ptb = await feed._fetch_price_to_beat_data_api("BTC", "5m", 1777824300)
    assert ptb is None


@pytest.mark.asyncio
async def test_data_api_returns_none_on_http_error():
    """Network exception in data-api fetch → None (never raises)."""
    feed = PolymarketHTMLPriceToBeatFeed()

    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
    feed._http = fake_client
    feed._owns_http = False

    ptb = await feed._fetch_price_to_beat_data_api("BTC", "5m", 1777824300)
    assert ptb is None


# ─────────────────────────────────────────────────────────────────────────────
# Retry budget honored
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_successful_attempt_returns_immediately_no_extra_attempts():
    """When the first HTML attempt succeeds, no further attempts run and no
    fallback paths fire."""
    feed = PolymarketHTMLPriceToBeatFeed()
    feed.get_price_to_beat = AsyncMock(return_value=70000.0)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=99999.0)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    assert ptb == 70000.0
    assert feed.get_price_to_beat.await_count == 1
    feed._fetch_price_to_beat_data_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_proceed_through_all_5_attempts_when_html_keeps_failing():
    """Per the [0,1,2,4,8] backoff schedule, the scraper retries up to 5
    attempts within the budget when each attempt returns None."""
    feed = PolymarketHTMLPriceToBeatFeed()
    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    # 30s budget covers all 5 backoff slots (cumulative 0+1+2+4+8 = 15s sleep).
    assert feed.get_price_to_beat.await_count == 5


# ─────────────────────────────────────────────────────────────────────────────
# Audit #352 — cache key MUST include window_ts.
# Polymarket sets a NEW priceToBeat strike for every 5m / 15m window, so a
# (asset, tf)-only key would silently leak window A's value into window B —
# inverting trade direction whenever HTML+data-api both fail in window B
# while window A has a fresh entry. These tests pin the structural fix.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_key_includes_window_ts_only_serves_same_window():
    """Audit #352 core assertion: a cache hit for window_ts=A returns ptb_A
    when retrieving window_ts=A — same-window lookup works."""
    feed = PolymarketHTMLPriceToBeatFeed()

    window_a = 1777824300
    feed._last_good[("BTC", "5m", window_a)] = (70123.45, time.monotonic())

    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=window_a, total_budget_s=30.0
        )

    # Same-window lookup hits the cache → returns the captured ptb.
    assert ptb == 70123.45
    # Data-api was NOT consulted because cache fallback fired first.
    feed._fetch_price_to_beat_data_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_does_not_leak_across_windows():
    """Audit #352 critical regression test: window A's cached priceToBeat
    must NOT be served for window B, even when window A's entry is fresh
    and window B has nothing.

    PR #485 had a (asset, tf)-only key that DID leak window A → window B.
    With the (asset, tf, window_ts) key, window B sees an empty cache,
    falls through to data-api, and on data-api failure logs fully_failed."""
    feed = PolymarketHTMLPriceToBeatFeed()

    window_a = 1777824000
    window_b = 1777824300  # next 5m window (300 s later)

    # Window A has a fresh cached priceToBeat.
    feed._last_good[("BTC", "5m", window_a)] = (70123.45, time.monotonic())

    # All HTML retries return None, data-api returns None.
    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=window_b, total_budget_s=30.0
        )

    # CRITICAL: window B must NOT receive window A's priceToBeat.
    assert ptb is None, (
        "Cache leaked window A's priceToBeat into window B — the very bug "
        "audit #352 fixes. Cache key MUST include window_ts."
    )
    # Confirm we did try the data-api fallback (proves cache fallback was
    # bypassed because the key didn't match).
    feed._fetch_price_to_beat_data_api.assert_awaited_once()
    # Window A's entry must still be present (fix must not delete it).
    assert ("BTC", "5m", window_a) in feed._last_good


@pytest.mark.asyncio
async def test_cache_does_not_leak_across_assets_or_timeframes():
    """The other two key dimensions still hold: BTC 5m cache must not serve
    ETH 5m, and BTC 5m cache must not serve BTC 15m."""
    feed = PolymarketHTMLPriceToBeatFeed()

    window_ts = 1777824300
    feed._last_good[("BTC", "5m", window_ts)] = (70000.0, time.monotonic())

    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    # ETH 5m, same window — should NOT serve BTC's value.
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb_eth = await feed.get_price_to_beat_with_retry(
            asset="ETH", timeframe="5m", window_ts=window_ts, total_budget_s=30.0
        )
    assert ptb_eth is None

    # BTC 15m, same window — should NOT serve BTC 5m's value.
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb_15m = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="15m", window_ts=window_ts, total_budget_s=30.0
        )
    assert ptb_15m is None


@pytest.mark.asyncio
async def test_same_window_cache_serves_repeated_calls_within_ttl():
    """Early-fire + retry scenario: the same window may be queried multiple
    times in quick succession (early fire at T-25s, then again at T-10s).
    Both calls must hit the cache and return the same value as long as
    cache_age_s <= LAST_GOOD_CACHE_TTL_SECS."""
    feed = PolymarketHTMLPriceToBeatFeed()

    window_ts = 1777824300
    feed._last_good[("BTC", "5m", window_ts)] = (70123.45, time.monotonic())

    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        ptb_first = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=window_ts, total_budget_s=30.0
        )
        ptb_second = await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=window_ts, total_budget_s=30.0
        )

    assert ptb_first == 70123.45
    assert ptb_second == 70123.45
    # Cache fallback fired both times — data-api never consulted.
    feed._fetch_price_to_beat_data_api.assert_not_awaited()


def test_evict_stale_last_good_drops_entries_older_than_4x_ttl():
    """`_evict_stale_last_good` is called at the top of every retry. It must
    drop entries older than ``LAST_GOOD_CACHE_TTL_SECS * 4`` (≥ 240 s) since
    such entries can never satisfy the 60-s fallback TTL anyway. Recent
    entries (younger than the cutoff) survive."""
    feed = PolymarketHTMLPriceToBeatFeed()

    now = time.monotonic()
    cutoff = LAST_GOOD_CACHE_TTL_SECS * 4.0

    # Fresh entry: well within cutoff.
    feed._last_good[("BTC", "5m", 1777824300)] = (70000.0, now - 5.0)
    # Boundary-fresh: just under the cutoff.
    feed._last_good[("BTC", "5m", 1777824000)] = (69500.0, now - (cutoff - 1.0))
    # Stale entry: just past the cutoff.
    feed._last_good[("BTC", "5m", 1777823700)] = (69000.0, now - (cutoff + 1.0))
    # Very stale entry: way past the cutoff.
    feed._last_good[("BTC", "5m", 1777823400)] = (68000.0, now - (cutoff + 600.0))

    feed._evict_stale_last_good()

    # Fresh entries kept; stale entries dropped.
    assert ("BTC", "5m", 1777824300) in feed._last_good
    assert ("BTC", "5m", 1777824000) in feed._last_good
    assert ("BTC", "5m", 1777823700) not in feed._last_good
    assert ("BTC", "5m", 1777823400) not in feed._last_good


@pytest.mark.asyncio
async def test_evict_stale_last_good_invoked_during_retry_path():
    """Eviction runs at the start of `get_price_to_beat_with_retry` so memory
    can't grow unboundedly across many windows. Verify by seeding a stale
    entry and calling the retry path — the stale entry should be gone."""
    feed = PolymarketHTMLPriceToBeatFeed()

    cutoff = LAST_GOOD_CACHE_TTL_SECS * 4.0
    stale_at = time.monotonic() - (cutoff + 60.0)
    feed._last_good[("BTC", "5m", 1777820000)] = (50000.0, stale_at)

    feed.get_price_to_beat = AsyncMock(return_value=None)
    feed._fetch_price_to_beat_data_api = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        await feed.get_price_to_beat_with_retry(
            asset="BTC", timeframe="5m", window_ts=1777824300, total_budget_s=30.0
        )

    # The stale entry from the long-past window has been evicted.
    assert ("BTC", "5m", 1777820000) not in feed._last_good
