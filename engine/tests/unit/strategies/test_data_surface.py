"""Tests for DataSurfaceManager -- builds FullDataSurface from mocked feeds."""

import sys
import os
import time

import pytest

# Ensure engine/ is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.data_surface import DataSurfaceManager, FullDataSurface


class FakeWindow:
    def __init__(self, **kwargs):
        self.asset = kwargs.get("asset", "BTC")
        self.window_ts = kwargs.get("window_ts", 1713000000)
        self.open_price = kwargs.get("open_price", 84000.0)
        self.eval_offset = kwargs.get("eval_offset", 120)
        self.up_price = kwargs.get("up_price", 0.55)
        self.down_price = kwargs.get("down_price", 0.45)


class FakeBinanceState:
    btc_price = 84500.0


class FakeTiingoFeed:
    latest_prices = {"BTC": 84480.0, "ETH": 3200.0}


class FakeChainlinkFeed:
    latest_prices = {"BTC": 84490.0, "SOL": 180.0}


class FakeCLOBFeed:
    latest_clob = {
        "clob_up_bid": 0.52,
        "clob_up_ask": 0.54,
        "clob_down_bid": 0.46,
        "clob_down_ask": 0.48,
        "clob_implied_up": 0.53,
    }


class FakeVPIN:
    current_vpin = 0.45
    regime = "NORMAL"


class FakeCG:
    oi_usd = 50_000_000.0
    funding_rate = 0.0001
    taker_buy_volume_1m = 1_200_000.0
    taker_sell_volume_1m = 800_000.0
    liq_total_usd_1m = 500_000.0
    liq_long_usd_1m = 300_000.0
    liq_short_usd_1m = 200_000.0
    long_short_ratio = 1.2


class TestDataSurfaceManager:

    def _make_manager(self) -> DataSurfaceManager:
        return DataSurfaceManager(
            v4_base_url="http://fake:8001",
            tiingo_feed=FakeTiingoFeed(),
            chainlink_feed=FakeChainlinkFeed(),
            clob_feed=FakeCLOBFeed(),
            vpin_calculator=FakeVPIN(),
            cg_feeds={"BTC": type("Feed", (), {"snapshot": FakeCG()})()},
            twap_tracker=None,
            binance_state=FakeBinanceState(),
        )

    def test_builds_surface_from_feeds(self):
        mgr = self._make_manager()
        window = FakeWindow()
        surface = mgr.get_surface(window, 120)

        assert isinstance(surface, FullDataSurface)
        assert surface.asset == "BTC"
        assert surface.eval_offset == 120
        assert surface.window_ts == 1713000000

    def test_price_deltas_calculated(self):
        mgr = self._make_manager()
        window = FakeWindow(open_price=84000.0)
        surface = mgr.get_surface(window, 120)

        # Binance delta: (84500 - 84000) / 84000
        assert surface.delta_binance is not None
        assert abs(surface.delta_binance - 500 / 84000) < 1e-6

        # Tiingo delta: (84480 - 84000) / 84000
        assert surface.delta_tiingo is not None
        assert abs(surface.delta_tiingo - 480 / 84000) < 1e-6

        # Chainlink delta: (84490 - 84000) / 84000
        assert surface.delta_chainlink is not None
        assert abs(surface.delta_chainlink - 490 / 84000) < 1e-6

    def test_primary_delta_tiingo_first(self):
        mgr = self._make_manager()
        window = FakeWindow(open_price=84000.0)
        surface = mgr.get_surface(window, 120)

        # Tiingo is first priority
        assert surface.delta_source == "tiingo_rest_candle"
        assert abs(surface.delta_pct - 480 / 84000) < 1e-6

    def test_clob_from_feed_cache(self):
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(), 120)

        assert surface.clob_up_bid == 0.52
        assert surface.clob_up_ask == 0.54
        assert surface.clob_down_bid == 0.46
        assert surface.clob_down_ask == 0.48
        assert surface.clob_implied_up == 0.53

    def test_vpin_and_regime(self):
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(), 120)

        assert surface.vpin == 0.45
        assert surface.regime == "NORMAL"

    def test_coinglass_fields(self):
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(), 120)

        assert surface.cg_oi_usd == 50_000_000.0
        assert surface.cg_funding_rate == 0.0001
        assert surface.cg_taker_buy_vol == 1_200_000.0
        assert surface.cg_taker_sell_vol == 800_000.0

    def test_gamma_prices_from_window(self):
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(up_price=0.55, down_price=0.45), 120)

        assert surface.gamma_up_price == 0.55
        assert surface.gamma_down_price == 0.45

    def test_surface_is_frozen(self):
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(), 120)

        with pytest.raises(AttributeError):
            surface.asset = "ETH"  # type: ignore

    def test_no_feeds_gives_defaults(self):
        mgr = DataSurfaceManager(v4_base_url="http://fake")
        surface = mgr.get_surface(FakeWindow(), 120)

        assert surface.current_price == 0.0
        assert surface.delta_tiingo is None
        assert surface.delta_chainlink is None
        assert surface.clob_up_bid is None
        assert surface.vpin == 0.0
        assert surface.regime == "UNKNOWN"

    def test_hour_utc_from_window_ts(self):
        mgr = self._make_manager()
        # 1713000000 = 2024-04-13 12:00:00 UTC
        surface = mgr.get_surface(FakeWindow(window_ts=1713000000), 120)
        assert surface.hour_utc is not None
