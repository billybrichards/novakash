from __future__ import annotations

from typing import Optional

import pytest

from engine.domain.value_objects import Asset, Timeframe, WindowMarket
from engine.use_cases.ports.market_discovery import MarketDiscoveryPort


class TestMarketDiscoveryPort:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            MarketDiscoveryPort()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_concrete_impl_signature(self):
        class Fake(MarketDiscoveryPort):
            async def find_window_market(
                self, asset: Asset, tf: Timeframe, window_ts: int
            ) -> Optional[WindowMarket]:
                return WindowMarket(
                    condition_id="0xabc",
                    up_token_id="1",
                    down_token_id="2",
                    market_slug=f"{asset.symbol.lower()}-updown-{tf.label}-{window_ts}",
                )

        d = Fake()
        m = await d.find_window_market(Asset("BTC"), Timeframe(300), 1776201300)
        assert m is not None
        assert m.market_slug == "btc-updown-5m-1776201300"
