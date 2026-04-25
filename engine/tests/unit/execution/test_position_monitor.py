"""Tests for PositionMonitor — mark-to-market stop-loss exit system.

Validates:
  - Position registration via on_fill()
  - Mark-to-market exit logic (CLOB bid vs fill price threshold)
  - Safety rails (min hold, no exit last N seconds)
  - Shadow mode (log but don't sell)
  - Position cleanup

See Hub note #240.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from execution.position_monitor import MonitoredPosition, PositionMonitor
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=None,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.60, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.38, clob_up_ask=0.40, clob_down_bid=0.53,
        clob_down_ask=0.55, clob_implied_up=0.40,
        gamma_up_price=0.40, gamma_down_price=0.60,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Registration tests ─────────────────────────────────────────────────────

def test_on_fill_registers_position():
    monitor = PositionMonitor()
    monitor.on_fill(
        strategy_id="v9_ensemble",
        window_ts=100,
        direction="DOWN",
        fill_price=0.55,
        fill_size=10.0,
        order_id="test-order-123",
        token_id="token-abc",
    )
    assert monitor.position_count == 1
    positions = monitor.get_open_positions()
    pos = positions["v9_ensemble:100"]
    assert pos.direction == "DOWN"
    assert pos.fill_price == 0.55
    assert pos.fill_size == 10.0


def test_remove_position():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "UP", 0.5, 10.0, "order1")
    assert monitor.position_count == 1
    monitor.remove_position("strat", 100)
    assert monitor.position_count == 0


# ── Mark-to-market exit evaluation tests ──────────────────────────────────

def test_mark_below_threshold_triggers_after_n_ticks():
    """Mark below 45% of fill for 10 ticks should trigger stop-loss."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60  # past min hold

    # DOWN position: mark = clob_down_bid
    # 0.20 / 0.55 = 36% of fill < 45% threshold
    surface = _make_surface(eval_offset=120, clob_down_bid=0.20)

    # Ticks 1-9: accumulate but don't trigger
    for i in range(9):
        result = monitor.evaluate_exit(
            "strat", 100, surface,
            exit_mark_min_pct=0.45,
            exit_mark_ticks=10,
        )
        assert result is None, f"Should not trigger on tick {i + 1}"
        assert pos.mark_loss_tick_count == i + 1

    # Tick 10: trigger
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=10,
    )
    assert result is not None
    assert "mark_stop_loss" in result
    assert "36%" in result  # 0.20/0.55 = 36%


def test_mark_above_threshold_resets_counter():
    """Mark recovering above threshold should reset the tick counter."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    bad_surface = _make_surface(eval_offset=120, clob_down_bid=0.20)
    ok_surface = _make_surface(eval_offset=120, clob_down_bid=0.50)

    # Accumulate 5 bad ticks
    for _ in range(5):
        monitor.evaluate_exit(
            "strat", 100, bad_surface,
            exit_mark_min_pct=0.45,
            exit_mark_ticks=10,
        )
    assert pos.mark_loss_tick_count == 5

    # One recovery tick resets counter
    monitor.evaluate_exit(
        "strat", 100, ok_surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=10,
    )
    assert pos.mark_loss_tick_count == 0


def test_mark_null_bid_skips():
    """If no CLOB bid data available, evaluate_exit returns None (can't price)."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # No bid data at all
    surface = _make_surface(
        eval_offset=120,
        clob_down_bid=None,
        clob_up_ask=None,
    )
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=10,
    )
    assert result is None
    assert pos.mark_loss_tick_count == 0  # should not increment


def test_min_hold_respected():
    """Position within min_hold_seconds should never trigger exit."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    # Just registered — within min_hold_seconds

    # Very bad mark that would trigger immediately
    surface = _make_surface(eval_offset=120, clob_down_bid=0.01)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_min_hold_seconds=45,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,  # would trigger on single tick
    )
    assert result is None


def test_no_exit_last_seconds_respected():
    """Should not exit when eval_offset < exit_no_exit_last_seconds."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # eval_offset 20 < 30 = too close to close
    surface = _make_surface(eval_offset=20, clob_down_bid=0.01)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_no_exit_last_seconds=30,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result is None


def test_up_position_uses_up_bid_or_1_minus_dn_ask():
    """UP position should use clob_up_bid as mark, falling back to 1 - clob_down_ask."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "UP", 0.45, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # Case 1: clob_up_bid available and > 0.01
    # up_bid = 0.15, fill = 0.45 => mark_pct = 0.333 < 0.45 => bad
    surface = _make_surface(eval_offset=120, clob_up_bid=0.15, clob_down_ask=0.90)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result is not None
    assert "mark_stop_loss" in result

    # Reset position
    pos.mark_loss_tick_count = 0

    # Case 2: clob_up_bid is None, fallback to 1 - clob_down_ask
    # 1 - 0.90 = 0.10, 0.10/0.45 = 22% < 45% => bad
    surface2 = _make_surface(eval_offset=120, clob_up_bid=None, clob_down_ask=0.90)
    result2 = monitor.evaluate_exit(
        "strat", 100, surface2,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result2 is not None
    assert "mark_stop_loss" in result2

    # Reset position
    pos.mark_loss_tick_count = 0

    # Case 3: clob_up_bid is 0.0 (too low, <= 0.01), fallback to 1 - down_ask
    # 1 - 0.55 = 0.45, 0.45/0.45 = 100% > 45% => safe
    surface3 = _make_surface(eval_offset=120, clob_up_bid=0.0, clob_down_ask=0.55)
    result3 = monitor.evaluate_exit(
        "strat", 100, surface3,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result3 is None


def test_down_position_uses_dn_bid_or_1_minus_up_ask():
    """DOWN position should use clob_down_bid as mark, falling back to 1 - clob_up_ask."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # Case 1: clob_down_bid available
    # dn_bid = 0.20, fill = 0.55 => mark_pct = 0.364 < 0.45 => bad
    surface = _make_surface(eval_offset=120, clob_down_bid=0.20, clob_up_ask=0.85)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result is not None
    assert "mark_stop_loss" in result

    pos.mark_loss_tick_count = 0

    # Case 2: clob_down_bid is None, fallback to 1 - clob_up_ask
    # 1 - 0.85 = 0.15, 0.15/0.55 = 27% < 45% => bad
    surface2 = _make_surface(eval_offset=120, clob_down_bid=None, clob_up_ask=0.85)
    result2 = monitor.evaluate_exit(
        "strat", 100, surface2,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result2 is not None
    assert "mark_stop_loss" in result2

    pos.mark_loss_tick_count = 0

    # Case 3: both None => can't price, skip
    surface3 = _make_surface(eval_offset=120, clob_down_bid=None, clob_up_ask=None)
    result3 = monitor.evaluate_exit(
        "strat", 100, surface3,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result3 is None


def test_evaluate_exit_returns_none_when_no_position():
    monitor = PositionMonitor()
    result = monitor.evaluate_exit("strat", 999, _make_surface())
    assert result is None


def test_evaluate_exit_returns_none_when_disabled():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60
    result = monitor.evaluate_exit(
        "strat", 100, _make_surface(clob_down_bid=0.01),
        exit_monitor_enabled=False,
    )
    assert result is None


def test_mark_at_exact_threshold_does_not_trigger():
    """Mark exactly at exit_mark_min_pct should NOT trigger (not strictly less)."""
    monitor = PositionMonitor()
    # fill=1.0 so mark_pct = mark directly
    monitor.on_fill("strat", 100, "DOWN", 1.0, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # 0.45/1.0 = 45% = exactly at threshold. < 0.45 is False (not strictly less)
    surface = _make_surface(eval_offset=120, clob_down_bid=0.45)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result is None
    assert pos.mark_loss_tick_count == 0


def test_mark_just_below_threshold_triggers():
    """Mark just below threshold should accumulate ticks."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 1.0, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 60

    # 0.449/1.0 = 44.9% < 45% threshold
    surface = _make_surface(eval_offset=120, clob_down_bid=0.449)
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_mark_min_pct=0.45,
        exit_mark_ticks=1,
    )
    assert result is not None
    assert "mark_stop_loss" in result


# ── Shadow mode test ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_exit_shadow_mode():
    """Shadow mode should log exit but not place sell order."""
    mock_alerter = MagicMock()
    mock_alerter.send_raw_message = AsyncMock()

    monitor = PositionMonitor(alerter=mock_alerter)
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1", "token-abc")

    result = await monitor.execute_exit(
        "strat", 100, "mark_stop_loss: mark=0.200 (36% of fill) for 10 ticks",
        exit_shadow_mode=True,
    )
    assert result is True
    assert monitor.position_count == 0  # removed from tracking
    # Should have called alerter
    mock_alerter.send_raw_message.assert_called_once()
    alert_text = mock_alerter.send_raw_message.call_args[0][0]
    assert "SHADOW" in alert_text


@pytest.mark.asyncio
async def test_execute_exit_no_position():
    """Execute exit on non-existent position returns False."""
    monitor = PositionMonitor()
    result = await monitor.execute_exit("strat", 999, "reason")
    assert result is False


# ── Sell-side execution test ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_exit_live_calls_sell():
    """Live mode should call place_sell_fak on the poly client."""
    mock_poly = MagicMock()
    mock_poly.place_sell_fak = AsyncMock(return_value={
        "filled": True,
        "size_matched": 10.0,
        "order_id": "sell-order-123",
    })

    monitor = PositionMonitor(poly_client=mock_poly)
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1", "token-abc")

    result = await monitor.execute_exit(
        "strat", 100, "mark_stop_loss: mark=0.200 (36% of fill) for 10 ticks",
        exit_shadow_mode=False,
    )
    assert result is True
    mock_poly.place_sell_fak.assert_called_once()
    assert monitor.position_count == 0
