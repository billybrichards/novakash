"""Unit test for CompositePriceGateway. No real HTTP."""
from __future__ import annotations

import pytest

from adapters.market_feed.composite_price_gateway import CompositePriceGateway
from domain.value_objects import Asset, Timeframe


class _FakeChainlink:
    def __init__(self, prices: dict[str, float]):
        self.latest_prices = prices


class _FakeBinance:
    def __init__(self, price: float | None):
        self.latest_price = price


class _FakeDB:
    def __init__(self, tiingo_latest: float | None = None):
        self._t = tiingo_latest

    async def get_latest_tiingo_price(self, asset: str) -> float | None:
        return self._t


@pytest.fixture
def gw():
    return CompositePriceGateway(
        chainlink_feed=_FakeChainlink({"ETH": 3000.0, "SOL": 150.0, "XRP": 0.5}),
        binance_spot_feed=_FakeBinance(50000.0),
        db=_FakeDB(tiingo_latest=99.0),
        tiingo_api_key="fake",
        http_session_factory=None,  # disables REST path in tests
    )


@pytest.mark.asyncio
async def test_btc_uses_binance(gw):
    p = await gw.get_current_price(Asset("BTC"))
    assert p == 50000.0


@pytest.mark.asyncio
async def test_eth_uses_chainlink(gw):
    p = await gw.get_current_price(Asset("ETH"))
    assert p == 3000.0


@pytest.mark.asyncio
async def test_xrp_uses_chainlink(gw):
    p = await gw.get_current_price(Asset("XRP"))
    assert p == 0.5


@pytest.mark.asyncio
async def test_falls_back_to_tiingo_db_if_chainlink_missing():
    gw = CompositePriceGateway(
        chainlink_feed=_FakeChainlink({}),  # empty
        binance_spot_feed=_FakeBinance(50000.0),
        db=_FakeDB(tiingo_latest=99.0),
        tiingo_api_key="fake",
        http_session_factory=None,
    )
    p = await gw.get_current_price(Asset("ETH"))
    assert p == 99.0


@pytest.mark.asyncio
async def test_returns_none_when_all_sources_missing():
    gw = CompositePriceGateway(
        chainlink_feed=_FakeChainlink({}),
        binance_spot_feed=_FakeBinance(None),
        db=_FakeDB(tiingo_latest=None),
        tiingo_api_key="fake",
        http_session_factory=None,
    )
    assert await gw.get_current_price(Asset("BTC")) is None
    assert await gw.get_current_price(Asset("ETH")) is None


@pytest.mark.asyncio
async def test_window_candle_falls_back_to_db_when_http_disabled(gw):
    candle = await gw.get_window_candle(Asset("BTC"), 1776201300, Timeframe(300))
    # With http_session_factory=None, candle should be None (no REST path).
    assert candle is None
