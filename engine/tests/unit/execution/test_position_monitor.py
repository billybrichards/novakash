"""Tests for PositionMonitor — post-fill exit monitoring system (Feature 4).

Validates:
  - Position registration via on_fill()
  - Exit evaluation logic (LGB flip, oracle flip, consecutive ticks)
  - Safety rails (min hold, no exit last N seconds)
  - Shadow mode (log but don't sell)
  - Position cleanup
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
        strategy_id="v8_champion_lgb_only",
        window_ts=100,
        direction="DOWN",
        fill_price=0.55,
        fill_size=10.0,
        order_id="test-order-123",
        token_id="token-abc",
    )
    assert monitor.position_count == 1
    positions = monitor.get_open_positions()
    pos = positions["v8_champion_lgb_only:100"]
    assert pos.direction == "DOWN"
    assert pos.fill_price == 0.55
    assert pos.fill_size == 10.0


def test_remove_position():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "UP", 0.5, 10.0, "order1")
    assert monitor.position_count == 1
    monitor.remove_position("strat", 100)
    assert monitor.position_count == 0


# ── Exit evaluation tests ────────────────────────────────────────────────

def test_evaluate_exit_returns_none_when_no_position():
    monitor = PositionMonitor()
    result = monitor.evaluate_exit("strat", 999, _make_surface())
    assert result is None


def test_evaluate_exit_returns_none_when_disabled():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    # Force filled_at to be in the past
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 30
    result = monitor.evaluate_exit(
        "strat", 100, _make_surface(),
        exit_monitor_enabled=False,
    )
    assert result is None


def test_evaluate_exit_returns_none_within_min_hold():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    # Just registered = within min_hold_seconds
    surface = _make_surface(probability_lgb=0.70)  # flipped to UP
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_min_hold_seconds=10,
    )
    assert result is None


def test_evaluate_exit_returns_none_in_last_seconds():
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 30
    # eval_offset < exit_no_exit_last_seconds = too close to close
    surface = _make_surface(
        eval_offset=20,  # less than 30
        probability_lgb=0.70,  # flipped
    )
    result = monitor.evaluate_exit(
        "strat", 100, surface,
        exit_no_exit_last_seconds=30,
    )
    assert result is None


def test_evaluate_exit_lgb_flip_accumulates():
    """LGB flip should accumulate consecutive_flip_count."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 30  # past min hold

    # Surface with LGB flipped to UP (DOWN bet, so this is a flip)
    flipped_surface = _make_surface(
        eval_offset=120,
        probability_lgb=0.70,  # UP
        delta_chainlink=-0.005,  # still DOWN-aligned oracles
        delta_tiingo=-0.004,
    )

    # Tick 1: flip count = 1
    result1 = monitor.evaluate_exit(
        "strat", 100, flipped_surface,
        exit_consecutive_flip_ticks=3,
    )
    assert result1 is None
    assert pos.consecutive_flip_count == 1

    # Tick 2: flip count = 2
    result2 = monitor.evaluate_exit(
        "strat", 100, flipped_surface,
        exit_consecutive_flip_ticks=3,
    )
    assert result2 is None
    assert pos.consecutive_flip_count == 2

    # Tick 3: flip count = 3 → exit triggered
    result3 = monitor.evaluate_exit(
        "strat", 100, flipped_surface,
        exit_consecutive_flip_ticks=3,
    )
    assert result3 is not None
    assert "exit_signal_flip" in result3
    assert "3 consecutive" in result3


def test_evaluate_exit_resets_on_non_flip():
    """Non-flip tick should reset the consecutive flip counter."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 30

    flipped = _make_surface(eval_offset=120, probability_lgb=0.70)
    aligned = _make_surface(eval_offset=120, probability_lgb=0.30)

    # Two flips then one aligned
    monitor.evaluate_exit("strat", 100, flipped, exit_consecutive_flip_ticks=3)
    monitor.evaluate_exit("strat", 100, flipped, exit_consecutive_flip_ticks=3)
    assert pos.consecutive_flip_count == 2

    monitor.evaluate_exit("strat", 100, aligned, exit_consecutive_flip_ticks=3)
    assert pos.consecutive_flip_count == 0


def test_evaluate_exit_oracle_flip():
    """Both chainlink and tiingo flipping should trigger oracle exit."""
    monitor = PositionMonitor()
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1")
    pos = monitor._positions["strat:100"]
    pos.filled_at_epoch = time.time() - 30

    # LGB still agrees DOWN but oracles both flipped UP
    oracle_flipped = _make_surface(
        eval_offset=120,
        probability_lgb=0.30,  # still DOWN
        delta_chainlink=+0.005,  # UP
        delta_tiingo=+0.004,  # UP
    )

    for _ in range(3):
        result = monitor.evaluate_exit(
            "strat", 100, oracle_flipped,
            exit_consecutive_flip_ticks=3,
            exit_lgb_flip_enabled=False,
            exit_oracle_flip_enabled=True,
        )

    assert result is not None
    assert "exit_signal_flip" in result


# ── Shadow mode test ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_exit_shadow_mode():
    """Shadow mode should log exit but not place sell order."""
    mock_alerter = MagicMock()
    mock_alerter.send_raw_message = AsyncMock()

    monitor = PositionMonitor(alerter=mock_alerter)
    monitor.on_fill("strat", 100, "DOWN", 0.55, 10.0, "order1", "token-abc")

    result = await monitor.execute_exit(
        "strat", 100, "test_reason",
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
        "strat", 100, "exit_signal_flip: 3 consecutive ticks flipped",
        exit_shadow_mode=False,
    )
    assert result is True
    mock_poly.place_sell_fak.assert_called_once()
    assert monitor.position_count == 0
