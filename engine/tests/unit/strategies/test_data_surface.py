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

    def test_primary_delta_chainlink_first_for_5m(self):
        mgr = self._make_manager()
        window = FakeWindow(open_price=84000.0)
        surface = mgr.get_surface(window, 120)

        # For 5m Polymarket markets chainlink is primary (it IS the resolution oracle)
        assert surface.delta_source == "chainlink"
        assert abs(surface.delta_pct - 490 / 84000) < 1e-6

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

    # ── Cedar shadow A/B fields (2026-04-21) ─────────────────────────────
    def test_cedar_fields_absent_when_no_cache(self):
        """With no cedar payload cached, cedar fields on the surface all
        default to None. This is the steady state before the ML box
        deploys /v2/probability/cedar — cedar strategies see None and
        SKIP with ``cedar_source_unavailable``."""
        mgr = self._make_manager()
        surface = mgr.get_surface(FakeWindow(), 120)
        assert surface.probability_up_cedar is None
        assert surface.probability_lgb_cedar is None
        assert surface.probability_classifier_cedar is None
        assert surface.v4_regime_cedar is None

    def test_cedar_fields_populated_from_cache(self):
        """Injecting a cached cedar payload directly populates the cedar
        fields on subsequent surfaces. Mirrors what the background
        refresh loop does after a successful /v2/probability/cedar call.
        """
        mgr = self._make_manager()
        mgr._cached_cedar["BTC"] = {
            "probability_up": 0.72,
            "probability_lgb": 0.68,
            "probability_classifier": 0.91,
            "regime": "volatile_trend",
        }
        mgr._cached_cedar_ts["BTC"] = time.time()

        surface = mgr.get_surface(FakeWindow(), 120)
        assert surface.probability_up_cedar == pytest.approx(0.72)
        assert surface.probability_lgb_cedar == pytest.approx(0.68)
        assert surface.probability_classifier_cedar == pytest.approx(0.91)
        assert surface.v4_regime_cedar == "volatile_trend"

    def test_cedar_fields_none_when_cache_stale(self):
        """Cedar cache older than 60s should be treated as absent so
        strategies don't trade against frozen model output. This mirrors
        the existing 60s staleness policy on the v4 snapshot cache."""
        mgr = self._make_manager()
        mgr._cached_cedar["BTC"] = {
            "probability_up": 0.72,
            "probability_lgb": 0.68,
            "probability_classifier": 0.91,
        }
        # 120s old — past the 60s staleness threshold.
        mgr._cached_cedar_ts["BTC"] = time.time() - 120

        surface = mgr.get_surface(FakeWindow(), 120)
        assert surface.probability_up_cedar is None
        assert surface.probability_lgb_cedar is None
        assert surface.probability_classifier_cedar is None

    def test_cedar_partial_payload_reads_available_fields(self):
        """Cedar endpoint returning only probability_up (no LGB / no
        classifier) should populate p_up and leave the rest None —
        strategies then see pegged_path1=unavailable and skip on bucket
        classification rather than the cedar_source gate. Preserves the
        forward-compat contract if the ML box ships an incremental v1."""
        mgr = self._make_manager()
        mgr._cached_cedar["BTC"] = {"probability_up": 0.45}
        mgr._cached_cedar_ts["BTC"] = time.time()

        surface = mgr.get_surface(FakeWindow(), 120)
        assert surface.probability_up_cedar == pytest.approx(0.45)
        assert surface.probability_lgb_cedar is None
        assert surface.probability_classifier_cedar is None


# ── Fix C: cedar fetch includes seconds_to_close (PR fix/engine-v2-probability-client-hardening) ──

@pytest.mark.asyncio
async def test_cedar_fetch_sends_seconds_to_close():
    """Fix C: _fetch_cedar_asset must include seconds_to_close in the GET
    query params.

    The GET /v2/probability/cedar endpoint has seconds_to_close as a required
    FastAPI Query parameter (ge=1, le=300). Previously the engine sent only
    ?asset=BTC, which caused 422 'Field required' on every cedar fetch call
    (833 / 2811 total /v2/probability calls = 29.6% error rate in prod logs).

    This test spins up an aiohttp mock server and verifies that after the fix,
    the cedar fetch includes seconds_to_close >= 1 in the query string.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    import json

    received_params = {}

    async def handle_cedar(request: web.Request) -> web.Response:
        received_params.update(dict(request.query))
        return web.Response(
            status=200,
            body=json.dumps({"probability_up": 0.65}),
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_get("/v2/probability/cedar", handle_cedar)
    server = TestServer(app)
    await server.start_server()
    try:
        import aiohttp as _aiohttp
        session = _aiohttp.ClientSession()
        mgr = DataSurfaceManager.__new__(DataSurfaceManager)
        mgr._session = session
        mgr._cedar_url = f"http://{server.host}:{server.port}/v2/probability/cedar"
        mgr._cedar_disabled = False
        mgr._cached_cedar = {}
        mgr._cached_cedar_ts = {}

        await mgr._fetch_cedar_asset("BTC")

        # Verify seconds_to_close is present and valid (ge=1).
        assert "seconds_to_close" in received_params, (
            f"cedar fetch did not send seconds_to_close. Query: {received_params}"
        )
        stc = int(received_params["seconds_to_close"])
        assert stc >= 1, f"seconds_to_close must be >= 1, got {stc}"
        assert stc <= 300, f"seconds_to_close must be <= 300, got {stc}"

        # Asset must also be present.
        assert received_params.get("asset") == "BTC"

        # Cache must have been updated on 200.
        assert mgr._cached_cedar.get("BTC") == {"probability_up": 0.65}

        await session.close()
    finally:
        await server.close()
