"""Tests for PositionMonitor hedge-exit (buy-opposite-and-hold) feature.

Pins the contract for the new hedge_* gate_params + the dual API
evaluate_hedge_exit / execute_hedge_exit.

The hedge mechanism (PR #X, 2026-04-27): when a multi-signal consensus
gate fires (LGB + chainlink-delta + tiingo-delta all point against our
position), the engine places an FAK BUY for the OPPOSITE token at the
ASK. Both sides are held to settlement. Net P&L per share is

    1.0  -  our_fill  -  opposite_ask

Default OFF and shadow_mode=True -- opt-in per strategy.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from execution.position_monitor import MonitoredPosition, PositionMonitor


# Fixtures -----------------------------------------------------------------


def make_monitor_with_position(
    direction: str = "UP",
    fill_price: float = 0.55,
    fill_size: float = 10.0,
    window_ts: int = 1_777_200_000,
    strategy_id: str = "v9_lgb_only",
    opposite_token_id: str = "0xtoken_opposite",
) -> PositionMonitor:
    pm = PositionMonitor()
    pm.on_fill(
        strategy_id=strategy_id,
        window_ts=window_ts,
        direction=direction,
        fill_price=fill_price,
        fill_size=fill_size,
        order_id="0xfeed",
        token_id="0xtoken_up" if direction == "UP" else "0xtoken_down",
        opposite_token_id=opposite_token_id,
    )
    return pm


def make_surface(
    *,
    offset: int = 150,
    window_ts: int = 1_777_200_000,
    clob_up_bid=None,
    clob_down_bid=None,
    clob_up_ask=None,
    clob_down_ask=None,
    lgb_p_up=None,
    lgb_dist=None,
    chainlink_delta=None,
    tiingo_delta=None,
    last_clob_update_ts=None,
    up_token_id: str = "0x_up",
    down_token_id: str = "0x_down",
):
    return SimpleNamespace(
        eval_offset=offset,
        window_ts=window_ts,
        clob_up_bid=clob_up_bid,
        clob_down_bid=clob_down_bid,
        clob_up_ask=clob_up_ask,
        clob_down_ask=clob_down_ask,
        lgb_p_up=lgb_p_up,
        lgb_dist=lgb_dist,
        chainlink_delta=chainlink_delta,
        tiingo_delta=tiingo_delta,
        last_clob_update_ts=(
            last_clob_update_ts if last_clob_update_ts is not None else time.time()
        ),
        up_token_id=up_token_id,
        down_token_id=down_token_id,
    )


def consensus_surface_for_up_position(
    *,
    offset: int = 150,
    lgb_p_opp: float = 0.90,
    lgb_dist: float = 0.40,
    chainlink_delta: float = -0.001,
    tiingo_delta: float = -0.0008,
    clob_down_ask: float = 0.30,
    clob_up_ask: float = 0.55,
):
    """Build surface where ALL signals agree opposite=DOWN for an UP holder."""
    return make_surface(
        offset=offset,
        lgb_p_up=1.0 - lgb_p_opp,
        lgb_dist=lgb_dist,
        chainlink_delta=chainlink_delta,
        tiingo_delta=tiingo_delta,
        clob_down_ask=clob_down_ask,
        clob_up_ask=clob_up_ask,
    )


HEDGE_DEFAULT_PARAMS = dict(
    hedge_exit_enabled=True,
    hedge_lgb_p_opposite_min=0.85,
    hedge_lgb_dist_min=0.20,
    hedge_chainlink_delta_opposite=True,
    hedge_tiingo_delta_opposite=True,
    hedge_consensus_consecutive_ticks=5,
    hedge_active_offset_min=90,
    hedge_active_offset_max=200,
    hedge_max_opposite_ask=0.45,
    hedge_min_guaranteed_profit_usd=0.50,
)


# 1. Disabled flag ---------------------------------------------------------


def test_hedge_disabled_returns_none():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position()
    params = dict(HEDGE_DEFAULT_PARAMS)
    params["hedge_exit_enabled"] = False
    out = pm.evaluate_hedge_exit("v9_lgb_only", 1_777_200_000, surface, **params)
    assert out is None


# 2. Single-tick consensus increments --------------------------------------


def test_consensus_count_increments_when_all_signals_agree():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position()
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None
    pos = pm._positions["v9_lgb_only:1777200000"]
    assert pos.hedge_consensus_count == 1


# 3. Disagreement resets count ---------------------------------------------


def test_consensus_count_resets_when_one_signal_disagrees():
    pm = make_monitor_with_position()
    pos_key = "v9_lgb_only:1777200000"
    for _ in range(4):
        pm.evaluate_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            consensus_surface_for_up_position(),
            **HEDGE_DEFAULT_PARAMS,
        )
    assert pm._positions[pos_key].hedge_consensus_count == 4
    surface = consensus_surface_for_up_position(chainlink_delta=0.001)
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None
    assert pm._positions[pos_key].hedge_consensus_count == 0


# 4. Five-tick consensus fires ---------------------------------------------


def test_fires_at_5_ticks_consensus():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position()
    last_out = None
    for i in range(5):
        last_out = pm.evaluate_hedge_exit(
            "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
        )
        if i < 4:
            assert last_out is None
    assert last_out is not None
    assert last_out["opposite_token_id"] == "0xtoken_opposite"
    assert last_out["size_to_buy"] == 10.0
    assert last_out["expected_guaranteed_profit"] == pytest.approx(1.50, abs=1e-6)
    assert last_out["opposite_ask"] == pytest.approx(0.30, abs=1e-6)


# 5. Economic gate: high opposite_ask --------------------------------------


def test_economic_gate_rejects_high_opposite_ask():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position(clob_down_ask=0.50)
    last = None
    for _ in range(5):
        last = pm.evaluate_hedge_exit(
            "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
        )
    assert last is None


# 6. Economic gate: low guaranteed profit ----------------------------------


def test_economic_gate_rejects_low_profit():
    pm = make_monitor_with_position(fill_size=5.0)
    surface = consensus_surface_for_up_position(clob_down_ask=0.40)
    last = None
    for _ in range(5):
        last = pm.evaluate_hedge_exit(
            "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
        )
    assert last is None


# 7. Outside offset window -------------------------------------------------


def test_outside_offset_window_no_eval():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position(offset=60)
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None
    pos = pm._positions["v9_lgb_only:1777200000"]
    assert pos.hedge_consensus_count == 0


# 8. Already hedged skips --------------------------------------------------


def test_already_hedged_skips():
    pm = make_monitor_with_position()
    pos = pm._positions["v9_lgb_only:1777200000"]
    pos.hedged = True
    surface = consensus_surface_for_up_position()
    for _ in range(10):
        out = pm.evaluate_hedge_exit(
            "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
        )
        assert out is None


# 9. LGB below threshold resets count --------------------------------------


def test_lgb_p_opposite_below_threshold_resets_count():
    pm = make_monitor_with_position()
    pos_key = "v9_lgb_only:1777200000"
    for _ in range(3):
        pm.evaluate_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            consensus_surface_for_up_position(),
            **HEDGE_DEFAULT_PARAMS,
        )
    assert pm._positions[pos_key].hedge_consensus_count == 3
    weak = consensus_surface_for_up_position(lgb_p_opp=0.70)
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, weak, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None
    assert pm._positions[pos_key].hedge_consensus_count == 0


# 10. Chainlink aligned (with us) resets count -----------------------------


def test_chainlink_delta_aligned_resets_count():
    pm = make_monitor_with_position()
    pos_key = "v9_lgb_only:1777200000"
    for _ in range(2):
        pm.evaluate_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            consensus_surface_for_up_position(),
            **HEDGE_DEFAULT_PARAMS,
        )
    assert pm._positions[pos_key].hedge_consensus_count == 2
    aligned = consensus_surface_for_up_position(chainlink_delta=0.002)
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, aligned, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None
    assert pm._positions[pos_key].hedge_consensus_count == 0


# 11. Shadow mode logs only ------------------------------------------------


def test_shadow_mode_logs_only():
    poly_calls = []

    class _PolyClient:
        async def place_market_order(self, **kwargs):
            poly_calls.append(kwargs)
            return {"filled": True, "size_matched": kwargs["size"], "order_id": "x"}

    pm = make_monitor_with_position()
    pm._poly_client = _PolyClient()
    pos = pm._positions["v9_lgb_only:1777200000"]

    instruction = {
        "opposite_token_id": pos.hedge_token_id_opposite,
        "size_to_buy": 10.0,
        "max_buy_price": 0.32,
        "expected_guaranteed_profit": 1.50,
        "opposite_ask": 0.30,
        "fill_price": 0.55,
        "consensus_ticks": 5,
        "eval_offset": 150,
        "direction_we_held": "UP",
        "opposite_direction": "DOWN",
    }

    ok = asyncio.run(
        pm.execute_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            instruction,
            hedge_shadow_mode=True,
        )
    )
    assert ok is True
    assert len(poly_calls) == 0
    assert pos.hedged is True


# 12. Real mode calls place_market_order -----------------------------------


def test_real_mode_calls_buy_fak():
    poly_calls = []

    class _PolyClient:
        async def place_market_order(self, **kwargs):
            poly_calls.append(kwargs)
            return {
                "filled": True,
                "size_matched": kwargs["size"],
                "order_id": "live-order-123",
            }

    pm = make_monitor_with_position()
    pm._poly_client = _PolyClient()
    pos = pm._positions["v9_lgb_only:1777200000"]

    instruction = {
        "opposite_token_id": pos.hedge_token_id_opposite,
        "size_to_buy": 10.0,
        "max_buy_price": 0.32,
        "expected_guaranteed_profit": 1.50,
        "opposite_ask": 0.30,
        "fill_price": 0.55,
        "consensus_ticks": 5,
        "eval_offset": 150,
        "direction_we_held": "UP",
        "opposite_direction": "DOWN",
    }

    ok = asyncio.run(
        pm.execute_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            instruction,
            hedge_shadow_mode=False,
            hedge_max_retries=1,
            hedge_buy_timeout_seconds=5,
        )
    )
    assert ok is True
    assert len(poly_calls) == 1
    call = poly_calls[0]
    assert call["token_id"] == "0xtoken_opposite"
    assert call["size"] == 10.0
    assert call["price"] == pytest.approx(0.32, abs=1e-6)
    assert call["order_type"] == "FAK"
    assert pos.hedged is True


# 13. Real-mode failure preserves state, retries ---------------------------


def test_real_mode_buy_fail_no_hedge_state_change():
    call_count = {"n": 0}

    class _PolyClient:
        async def place_market_order(self, **kwargs):
            call_count["n"] += 1
            return {"filled": False, "size_matched": 0.0, "order_id": None}

    pm = make_monitor_with_position()
    pm._poly_client = _PolyClient()
    pos = pm._positions["v9_lgb_only:1777200000"]

    instruction = {
        "opposite_token_id": pos.hedge_token_id_opposite,
        "size_to_buy": 10.0,
        "max_buy_price": 0.32,
        "expected_guaranteed_profit": 1.50,
        "opposite_ask": 0.30,
        "fill_price": 0.55,
        "consensus_ticks": 5,
        "eval_offset": 150,
        "direction_we_held": "UP",
        "opposite_direction": "DOWN",
    }

    ok = asyncio.run(
        pm.execute_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            instruction,
            hedge_shadow_mode=False,
            hedge_max_retries=2,
        )
    )
    assert ok is False
    assert call_count["n"] == 2
    assert pos.hedged is False


# 14. Position not popped after hedge --------------------------------------


def test_position_not_popped_after_hedge():
    pm = make_monitor_with_position()
    pos = pm._positions["v9_lgb_only:1777200000"]
    instruction = {
        "opposite_token_id": pos.hedge_token_id_opposite,
        "size_to_buy": 10.0,
        "max_buy_price": 0.32,
        "expected_guaranteed_profit": 1.50,
        "opposite_ask": 0.30,
        "fill_price": 0.55,
        "consensus_ticks": 5,
        "eval_offset": 150,
        "direction_we_held": "UP",
        "opposite_direction": "DOWN",
    }
    asyncio.run(
        pm.execute_hedge_exit(
            "v9_lgb_only",
            1_777_200_000,
            instruction,
            hedge_shadow_mode=True,
        )
    )
    assert "v9_lgb_only:1777200000" in pm._positions
    assert pm._positions["v9_lgb_only:1777200000"].hedged is True


# 15. Stale CLOB data skips ------------------------------------------------


def test_stale_clob_skips_eval():
    pm = make_monitor_with_position()
    surface = consensus_surface_for_up_position(offset=150)
    surface.last_clob_update_ts = time.time() - 10
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None


# 16. Missing opposite_token_id skips --------------------------------------


def test_missing_opposite_token_id_skips():
    pm = make_monitor_with_position(opposite_token_id="")
    surface = consensus_surface_for_up_position()
    out = pm.evaluate_hedge_exit(
        "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
    )
    assert out is None


# 17. DOWN position uses up_ask as opposite -------------------------------


def test_down_position_reads_up_ask_as_opposite():
    pm = make_monitor_with_position(direction="DOWN", fill_price=0.50)
    surface = make_surface(
        offset=150,
        lgb_p_up=0.90,
        lgb_dist=0.40,
        chainlink_delta=0.002,
        tiingo_delta=0.0015,
        clob_up_ask=0.30,
        clob_down_ask=0.55,
    )
    last = None
    for _ in range(5):
        last = pm.evaluate_hedge_exit(
            "v9_lgb_only", 1_777_200_000, surface, **HEDGE_DEFAULT_PARAMS
        )
    assert last is not None
    assert last["expected_guaranteed_profit"] == pytest.approx(2.0, abs=1e-6)
    assert last["opposite_direction"] == "UP"
