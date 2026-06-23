"""Test PriceGateway is an abstract port with the right signature."""
from __future__ import annotations

import inspect

import pytest

from engine.domain.value_objects import Asset, Timeframe, PriceCandle
from engine.use_cases.ports.price_gateway import PriceGateway


class TestPriceGatewayPort:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            PriceGateway()  # type: ignore[abstract]

    def test_has_get_current_price(self):
        assert hasattr(PriceGateway, "get_current_price")

    def test_has_get_window_candle(self):
        assert hasattr(PriceGateway, "get_window_candle")

    @pytest.mark.asyncio
    async def test_concrete_impl_signature_matches(self):
        class Fake(PriceGateway):
            async def get_current_price(self, asset: Asset):
                return 50000.0

            async def get_window_candle(self, asset: Asset, window_ts: int, tf: Timeframe):
                return PriceCandle(50000.0, 50100.0, "fake")

        g = Fake()
        assert await g.get_current_price(Asset("BTC")) == 50000.0
        c = await g.get_window_candle(Asset("BTC"), 1776201300, Timeframe(300))
        assert c.close_price == 50100.0
