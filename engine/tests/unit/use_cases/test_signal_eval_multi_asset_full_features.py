"""Tests that registry._write_window_trace populates full feature columns
for ETH/SOL/XRP signal_evaluations rows (closes the placeholder-rows gap
diagnosed in RDS note #807).

The stub writer bug: update_signal_evaluations_lgb_* sidecars INSERT rows
with only (window_ts, asset, timeframe, eval_offset, probability_X).  For
non-BTC assets no subsequent write_signal_evaluation call filled in CLOB /
vpin / delta_* / cg_* / v2 columns — leaving them NULL.  The fix adds a
write_signal_evaluation call inside _write_window_trace for asset != BTC.

Test coverage:
- ETH, XRP, SOL each produce a write_signal_evaluation call with
  CLOB + vpin + delta_* + cg_* fields populated (or passthrough-None
  when the surface field is genuinely None).
- BTC is NOT written by this path (to avoid race with existing BTC writers).
- The write includes correct asset / window_ts / eval_offset identity.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_surface(
    asset: str = "ETH",
    window_ts: int = 1712345600,
    eval_offset: int = 120,
    clob_up_bid=0.52,
    clob_up_ask=0.54,
    clob_down_bid=0.44,
    clob_down_ask=0.46,
    vpin: float = 0.55,
    regime: str = "NORMAL",
    delta_pct: float = 0.04,
    delta_binance=0.05,
    delta_tiingo=0.04,
    delta_chainlink=0.03,
    delta_source: str = "tiingo_rest_candle",
    current_price: float = 2500.0,
    open_price: float = 2450.0,
    cg_liq_long=1_200_000.0,
    cg_liq_short=800_000.0,
    cg_taker_buy_vol=5_000_000.0,
    cg_taker_sell_vol=4_200_000.0,
    cg_funding_rate=0.0001,
    v2_probability_up=0.72,
) -> MagicMock:
    """Build a MagicMock that quacks like FullDataSurface."""
    s = MagicMock()
    s.asset = asset
    s.timescale = "5m"
    s.window_ts = window_ts
    s.eval_offset = eval_offset
    s.assembled_at = float(window_ts)
    s.current_price = current_price
    s.open_price = open_price
    s.delta_pct = delta_pct
    s.delta_binance = delta_binance
    s.delta_tiingo = delta_tiingo
    s.delta_chainlink = delta_chainlink
    s.delta_source = delta_source
    s.vpin = vpin
    s.regime = regime
    s.clob_up_bid = clob_up_bid
    s.clob_up_ask = clob_up_ask
    s.clob_down_bid = clob_down_bid
    s.clob_down_ask = clob_down_ask
    s.cg_liq_long = cg_liq_long
    s.cg_liq_short = cg_liq_short
    s.cg_taker_buy_vol = cg_taker_buy_vol
    s.cg_taker_sell_vol = cg_taker_sell_vol
    s.cg_funding_rate = cg_funding_rate
    s.v2_probability_up = v2_probability_up
    return s


async def _invoke_trace(registry, surface):
    """Invoke _write_window_trace and flush all created tasks."""
    from strategies.registry import StrategyRegistry

    bound = StrategyRegistry._write_window_trace.__get__(registry)
    bound(surface)
    # Let created asyncio tasks run (two passes to handle nested tasks)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _make_db():
    """Return a db mock with all methods _write_window_trace might call as AsyncMock.

    Every method that _write_window_trace wraps in asyncio.create_task must return
    a coroutine; AsyncMock satisfies that requirement.
    """
    db = MagicMock()
    # Canonical rich-feature writer (the one we're testing)
    db.write_signal_evaluation = AsyncMock()
    # All sidecar writers called by _write_window_trace (MagicMock returns a new
    # MagicMock, not a coroutine, which breaks create_task — set each to AsyncMock)
    db.update_window_surface_fields = AsyncMock()
    db.update_window_ensemble_fields = AsyncMock()
    db.update_signal_evaluations_lgb_v12 = AsyncMock()
    db.update_signal_evaluations_lgb_v9_1 = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2_post_iso = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2_eth = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_eth = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_eth_pure = AsyncMock()
    db.update_signal_evaluations_lgb_v9_3_btc = AsyncMock()
    db.update_signal_evaluations_lgb_v9_3_btc_pure = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2_pure = AsyncMock()
    db.update_signal_evaluations_lgb_v12_pure = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp_pure = AsyncMock()
    db.update_signal_evaluations_tickformer = AsyncMock()
    db.update_signal_evaluations_tickformer_v16 = AsyncMock()
    db.update_signal_evaluations_tickformer_v17 = AsyncMock()
    db.update_signal_evaluations_tickformer_v18 = AsyncMock()
    db.update_signal_evaluations_v2_meta_gate = AsyncMock()
    db.update_signal_evaluations_v9_2_meta_gate = AsyncMock()
    db.update_signal_evaluations_v12_meta_gate = AsyncMock()
    return db


def _make_registry(db_client=None):
    """Return a registry-like MagicMock with only the attributes _write_window_trace needs."""
    from strategies.registry import StrategyRegistry

    registry = MagicMock()
    registry._db = db_client if db_client is not None else _make_db()
    # Bind the real _log_async_write_error so task callbacks work
    registry._log_async_write_error = StrategyRegistry._log_async_write_error.__get__(registry)
    # Provide the other attributes _write_window_trace references
    registry._surface_trace_data = MagicMock(return_value={})
    registry._trace_repo = MagicMock()
    registry._trace_repo.write_window_evaluation_trace = AsyncMock()
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNonBtcFullFeatureWrite:
    """_write_window_trace writes full signal_evaluations row for ETH/SOL/XRP."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_write_signal_evaluation_called_for_non_btc(self, asset):
        """write_signal_evaluation is called at least once for non-BTC assets."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset)
        await _invoke_trace(registry, surface)

        assert db.write_signal_evaluation.called, (
            f"write_signal_evaluation not called for asset={asset}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_clob_fields_populated_for_non_btc(self, asset):
        """CLOB fields from surface reach the write_signal_evaluation dict."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(
            asset=asset,
            clob_up_bid=0.51,
            clob_up_ask=0.53,
            clob_down_bid=0.43,
            clob_down_ask=0.45,
        )
        await _invoke_trace(registry, surface)

        assert db.write_signal_evaluation.called
        written = db.write_signal_evaluation.call_args[0][0]
        assert written["clob_up_bid"] == 0.51
        assert written["clob_up_ask"] == 0.53
        assert written["clob_down_bid"] == 0.43
        assert written["clob_down_ask"] == 0.45

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_vpin_delta_regime_populated_for_non_btc(self, asset):
        """vpin / delta_pct / regime / delta_source are passed through."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(
            asset=asset,
            vpin=0.62,
            regime="TRANSITION",
            delta_pct=0.07,
            delta_binance=0.08,
            delta_tiingo=0.07,
            delta_chainlink=0.06,
            delta_source="chainlink",
        )
        await _invoke_trace(registry, surface)

        written = db.write_signal_evaluation.call_args[0][0]
        assert written["vpin"] == pytest.approx(0.62)
        assert written["regime"] == "TRANSITION"
        assert written["delta_pct"] == pytest.approx(0.07)
        assert written["delta_binance"] == pytest.approx(0.08)
        assert written["delta_tiingo"] == pytest.approx(0.07)
        assert written["delta_chainlink"] == pytest.approx(0.06)
        assert written["delta_source"] == "chainlink"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_cg_fields_populated_for_non_btc(self, asset):
        """CoinGlass fields from surface reach the write dict."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(
            asset=asset,
            cg_liq_long=1_500_000.0,
            cg_liq_short=900_000.0,
            cg_taker_buy_vol=6_000_000.0,
            cg_taker_sell_vol=4_000_000.0,
            cg_funding_rate=0.0002,
        )
        await _invoke_trace(registry, surface)

        written = db.write_signal_evaluation.call_args[0][0]
        assert written["cg_liq_long_usd"] == pytest.approx(1_500_000.0)
        assert written["cg_liq_short_usd"] == pytest.approx(900_000.0)
        assert written["cg_taker_buy_usd"] == pytest.approx(6_000_000.0)
        assert written["cg_taker_sell_usd"] == pytest.approx(4_000_000.0)
        assert written["cg_funding_rate"] == pytest.approx(0.0002)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_v2_probability_populated_for_non_btc(self, asset):
        """v2_probability_up and derived direction reach the write dict."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset, v2_probability_up=0.78)
        await _invoke_trace(registry, surface)

        written = db.write_signal_evaluation.call_args[0][0]
        assert written["v2_probability_up"] == pytest.approx(0.78)
        assert written["v2_direction"] == "UP"
        assert written["v2_high_conf"] is True  # 0.78 > 0.65

    @pytest.mark.asyncio
    async def test_btc_does_not_call_write_signal_evaluation(self):
        """BTC asset must NOT trigger the trace-time write_signal_evaluation."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(asset="BTC")
        await _invoke_trace(registry, surface)

        db.write_signal_evaluation.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP"])
    async def test_null_clob_passes_through_cleanly(self, asset):
        """When CLOB fields are None on the surface, write dict carries None (no crash)."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(
            asset=asset,
            clob_up_bid=None,
            clob_up_ask=None,
            clob_down_bid=None,
            clob_down_ask=None,
        )
        await _invoke_trace(registry, surface)

        assert db.write_signal_evaluation.called
        written = db.write_signal_evaluation.call_args[0][0]
        assert written["clob_up_bid"] is None
        assert written["clob_up_ask"] is None
        assert written["clob_down_bid"] is None
        assert written["clob_down_ask"] is None
        assert written["clob_spread"] is None
        assert written["clob_mid"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP"])
    async def test_identity_fields_correct(self, asset):
        """window_ts, asset, timeframe, eval_offset are set correctly."""
        db = _make_db()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset, window_ts=1712345700, eval_offset=60)
        await _invoke_trace(registry, surface)

        written = db.write_signal_evaluation.call_args[0][0]
        assert written["asset"] == asset
        assert written["window_ts"] == 1712345700
        assert written["eval_offset"] == 60
        assert written["timeframe"] == "5m"
