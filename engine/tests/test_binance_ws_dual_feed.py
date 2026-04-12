"""
Tests for BinanceWebSocketFeed dual venue support (spot + futures).

Verifies:
  - venue="spot" generates stream.binance.com URL with aggTrade only
  - venue="futures" generates fstream.binance.com URL with all streams
  - Invalid venue raises ValueError
  - Both feeds can coexist without interference
  - Spot feed updates btc_spot_price via aggregator
"""

import pytest
import asyncio
from decimal import Decimal
from datetime import datetime

from data.feeds.binance_ws import (  # noqa: E402
    BinanceWebSocketFeed,
    BINANCE_SPOT_WSS,
    BINANCE_FUTURES_WSS,
)
from data.models import AggTrade, MarketState  # noqa: E402
from data.aggregator import MarketAggregator  # noqa: E402


# ── URL generation tests ─────────────────────────────────────────────────────


class TestBinanceWSStreamURL:
    """Test that venue parameter produces the correct WebSocket URLs."""

    def test_futures_url_uses_fstream(self):
        feed = BinanceWebSocketFeed(symbol="btcusdt", venue="futures")
        url = feed._stream_url
        assert url.startswith(BINANCE_FUTURES_WSS)
        assert "fstream.binance.com" in url

    def test_futures_url_includes_all_streams(self):
        feed = BinanceWebSocketFeed(symbol="btcusdt", venue="futures")
        url = feed._stream_url
        assert "btcusdt@aggTrade" in url
        assert "btcusdt@depth20@100ms" in url
        assert "btcusdt@forceOrder" in url

    def test_spot_url_uses_stream(self):
        feed = BinanceWebSocketFeed(symbol="btcusdt", venue="spot")
        url = feed._stream_url
        assert url.startswith(BINANCE_SPOT_WSS)
        assert "stream.binance.com:9443" in url

    def test_spot_url_only_aggtrade(self):
        feed = BinanceWebSocketFeed(symbol="btcusdt", venue="spot")
        url = feed._stream_url
        assert "btcusdt@aggTrade" in url
        # Spot should NOT have depth or forceOrder
        assert "depth20" not in url
        assert "forceOrder" not in url

    def test_default_venue_is_futures(self):
        feed = BinanceWebSocketFeed(symbol="btcusdt")
        assert feed.venue == "futures"
        assert "fstream.binance.com" in feed._stream_url

    def test_invalid_venue_raises(self):
        with pytest.raises(ValueError, match="venue must be"):
            BinanceWebSocketFeed(symbol="btcusdt", venue="invalid")

    def test_venue_stored_on_instance(self):
        spot = BinanceWebSocketFeed(symbol="btcusdt", venue="spot")
        futures = BinanceWebSocketFeed(symbol="btcusdt", venue="futures")
        assert spot.venue == "spot"
        assert futures.venue == "futures"


# ── Coexistence tests ────────────────────────────────────────────────────────


class TestDualFeedCoexistence:
    """Test that spot and futures feeds can be created independently."""

    def test_two_feeds_different_urls(self):
        spot = BinanceWebSocketFeed(symbol="btcusdt", venue="spot")
        futures = BinanceWebSocketFeed(symbol="btcusdt", venue="futures")
        assert spot._stream_url != futures._stream_url
        assert "stream.binance.com:9443" in spot._stream_url
        assert "fstream.binance.com" in futures._stream_url

    def test_two_feeds_independent_state(self):
        spot = BinanceWebSocketFeed(symbol="btcusdt", venue="spot")
        futures = BinanceWebSocketFeed(symbol="btcusdt", venue="futures")
        # Each has its own connected state
        assert not spot.connected
        assert not futures.connected
        assert spot._running is False
        assert futures._running is False


# ── Aggregator integration tests ─────────────────────────────────────────────


class TestAggregatorSpotPrice:
    """Test that spot trades update btc_spot_price separately from btc_price."""

    @pytest.mark.asyncio
    async def test_spot_trade_updates_btc_spot_price(self):
        agg = MarketAggregator()
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("84500.00"),
            quantity=Decimal("0.1"),
            is_buyer_maker=False,
            trade_time=datetime.utcnow(),
        )
        await agg.on_spot_trade(trade)
        state = await agg.get_state()
        assert state.btc_spot_price == Decimal("84500.00")
        # btc_price (futures) should still be None
        assert state.btc_price is None

    @pytest.mark.asyncio
    async def test_futures_trade_updates_btc_price_not_spot(self):
        agg = MarketAggregator()
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("84600.00"),
            quantity=Decimal("0.1"),
            is_buyer_maker=False,
            trade_time=datetime.utcnow(),
        )
        await agg.on_agg_trade(trade)
        state = await agg.get_state()
        assert state.btc_price == Decimal("84600.00")
        # btc_spot_price should still be None
        assert state.btc_spot_price is None

    @pytest.mark.asyncio
    async def test_both_feeds_update_independently(self):
        agg = MarketAggregator()
        futures_trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("84600.00"),
            quantity=Decimal("0.1"),
            is_buyer_maker=False,
            trade_time=datetime.utcnow(),
        )
        spot_trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("84500.00"),
            quantity=Decimal("0.1"),
            is_buyer_maker=False,
            trade_time=datetime.utcnow(),
        )
        await agg.on_agg_trade(futures_trade)
        await agg.on_spot_trade(spot_trade)
        state = await agg.get_state()
        assert state.btc_price == Decimal("84600.00")
        assert state.btc_spot_price == Decimal("84500.00")
        # Spot and futures prices differ (basis)
        assert state.btc_spot_price != state.btc_price


class TestMarketStateSpotField:
    """Test that MarketState has the btc_spot_price field."""

    def test_default_is_none(self):
        state = MarketState()
        assert state.btc_spot_price is None

    def test_can_set_spot_price(self):
        state = MarketState(btc_spot_price=Decimal("84500.00"))
        assert state.btc_spot_price == Decimal("84500.00")
