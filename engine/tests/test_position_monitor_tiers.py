"""Tests for PositionMonitor multi-tier exit ladder + signal-flip detector
(PR #402, 2026-04-27).

Pins the contract for the new exit_tiers + flip_* gate_params.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from execution.position_monitor import (
    ExitTier,
    MonitoredPosition,
    PositionMonitor,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def make_monitor_with_position(
    direction: str = "UP",
    fill_price: float = 0.55,
    window_ts: int = 1_777_200_000,
    strategy_id: str = "v9_lgb_only",
) -> PositionMonitor:
    pm = PositionMonitor()
    pm.on_fill(
        strategy_id=strategy_id,
        window_ts=window_ts,
        direction=direction,
        fill_price=fill_price,
        fill_size=10.0,
        order_id="0xfeed",
        token_id="0xtoken_up" if direction == "UP" else "0xtoken_down",
    )
    return pm


def make_surface(
    *,
    offset: int,
    window_ts: int = 1_777_200_000,
    clob_up_bid=None,
    clob_down_bid=None,
    clob_up_ask=None,
    clob_down_ask=None,
    lgb_p_up=None,
    lgb_dist=None,
    last_clob_update_ts=None,
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
        last_clob_update_ts=(
            last_clob_update_ts
            if last_clob_update_ts is not None
            else time.time()
        ),
    )


THREE_TIERS = [
    {"name": "tier1", "start_offset": 200, "end_offset": 120,
     "mark_pct": 0.50, "mark_ticks": 5},
    {"name": "tier2", "start_offset": 120, "end_offset": 60,
     "mark_pct": 0.55, "mark_ticks": 4},
    {"name": "tier3", "start_offset": 60, "end_offset": 30,
     "mark_pct": 0.70, "mark_ticks": 3},
]


KEY = "v9_lgb_only:1777200000"


def _check(pm, **kwargs):
    """Helper wrapping evaluate_exit with sensible defaults."""
    defaults = dict(
        strategy_id="v9_lgb_only",
        window_ts=1_777_200_000,
        exit_tiers=THREE_TIERS,
    )
    defaults.update(kwargs)
    return pm.evaluate_exit(**defaults)


# ── Tier resolution ─────────────────────────────────────────────────────


class TestTierResolution:
    def test_parses_yaml_dicts_to_exit_tiers(self):
        out = PositionMonitor._resolve_tiers(THREE_TIERS, 48, 30, 0.45, 6)
        assert len(out) == 3
        assert all(isinstance(t, ExitTier) for t in out)
        assert out[0].name == "tier1"
        assert out[2].mark_pct == 0.70
        assert out[2].mark_ticks == 3

    def test_legacy_fallback_when_no_tiers(self):
        out = PositionMonitor._resolve_tiers(None, 48, 30, 0.45, 6)
        assert len(out) == 1
        assert out[0].name == "legacy"
        assert out[0].mark_pct == 0.45
        assert out[0].mark_ticks == 6

    def test_skips_malformed_tier_entries(self):
        bad = [
            {"name": "ok", "start_offset": 100, "end_offset": 50,
             "mark_pct": 0.5, "mark_ticks": 3},
            {"name": "missing_keys"},
        ]
        out = PositionMonitor._resolve_tiers(bad, 48, 30, 0.45, 6)
        assert len(out) == 1
        assert out[0].name == "ok"

    def test_all_malformed_falls_back_to_legacy(self):
        out = PositionMonitor._resolve_tiers([{"name": "x"}], 48, 30, 0.45, 6)
        assert len(out) == 1
        assert out[0].name == "legacy"


# ── Current tier lookup ─────────────────────────────────────────────────


class TestFindCurrentTier:
    def test_finds_tier1_at_t180(self):
        tiers = [ExitTier(**t) for t in THREE_TIERS]
        t = PositionMonitor._find_current_tier(tiers, 180)
        assert t is not None and t.name == "tier1"

    def test_finds_tier3_at_t45(self):
        tiers = [ExitTier(**t) for t in THREE_TIERS]
        t = PositionMonitor._find_current_tier(tiers, 45)
        assert t is not None and t.name == "tier3"

    def test_pre_t200_returns_none(self):
        tiers = [ExitTier(**t) for t in THREE_TIERS]
        assert PositionMonitor._find_current_tier(tiers, 250) is None

    def test_past_t30_returns_none(self):
        tiers = [ExitTier(**t) for t in THREE_TIERS]
        assert PositionMonitor._find_current_tier(tiers, 20) is None

    def test_boundary_inclusive_first_match_wins(self):
        tiers = [ExitTier(**t) for t in THREE_TIERS]
        t = PositionMonitor._find_current_tier(tiers, 120)
        assert t is not None and t.name == "tier1"


# ── Mark-based tier exits ───────────────────────────────────────────────


class TestTierExits:
    def test_tier1_triggers_on_5_ticks_below_50pct(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(4):
            assert _check(
                pm, surface=make_surface(offset=180, clob_up_bid=0.27)
            ) is None
        reason = _check(
            pm, surface=make_surface(offset=180, clob_up_bid=0.27)
        )
        assert reason is not None
        assert "tier1_mark_stop_loss" in reason

    def test_tier1_does_not_trigger_above_50pct(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(10):
            r = _check(
                pm, surface=make_surface(offset=180, clob_up_bid=0.305)
            )
            assert r is None

    def test_tier_transition_resets_count(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(4):
            _check(pm, surface=make_surface(offset=180, clob_up_bid=0.20))
        pos = pm.get_open_positions()[KEY]
        assert pos.mark_loss_tick_count == 4
        assert pos.current_tier_name == "tier1"

        _check(pm, surface=make_surface(offset=100, clob_up_bid=0.20))
        pos = pm.get_open_positions()[KEY]
        assert pos.current_tier_name == "tier2"
        assert pos.mark_loss_tick_count == 1

    def test_recovery_resets_count(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(3):
            _check(pm, surface=make_surface(offset=180, clob_up_bid=0.20))
        _check(pm, surface=make_surface(offset=180, clob_up_bid=0.45))
        pos = pm.get_open_positions()[KEY]
        assert pos.mark_loss_tick_count == 0

    def test_past_t30_no_exit(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(10):
            r = _check(pm, surface=make_surface(offset=20, clob_up_bid=0.05))
            assert r is None

    def test_pre_t200_no_exit(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(10):
            r = _check(pm, surface=make_surface(offset=250, clob_up_bid=0.05))
            assert r is None

    def test_tier3_aggressive_3_ticks(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(2):
            assert _check(
                pm, surface=make_surface(offset=45, clob_up_bid=0.33)
            ) is None
        reason = _check(
            pm, surface=make_surface(offset=45, clob_up_bid=0.33)
        )
        assert reason is not None and "tier3" in reason


# ── Legacy fallback ─────────────────────────────────────────────────────


class TestLegacyFallback:
    def test_no_exit_tiers_uses_legacy_single_tier(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(2):
            assert pm.evaluate_exit(
                strategy_id="v9_lgb_only",
                window_ts=1_777_200_000,
                surface=make_surface(offset=40, clob_up_bid=0.30),
                exit_eval_start_offset=48,
                exit_eval_end_offset=30,
                exit_mark_min_pct=0.70,
                exit_mark_ticks=3,
            ) is None
        reason = pm.evaluate_exit(
            strategy_id="v9_lgb_only",
            window_ts=1_777_200_000,
            surface=make_surface(offset=40, clob_up_bid=0.30),
            exit_eval_start_offset=48,
            exit_eval_end_offset=30,
            exit_mark_min_pct=0.70,
            exit_mark_ticks=3,
        )
        assert reason is not None and "legacy" in reason


# ── Stale mark guard ────────────────────────────────────────────────────


class TestStaleMarkGuard:
    def test_stale_mark_skips_eval_and_does_not_increment(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        stale_ts = time.time() - 10.0
        for _ in range(10):
            r = _check(
                pm,
                surface=make_surface(
                    offset=180,
                    clob_up_bid=0.05,
                    last_clob_update_ts=stale_ts,
                ),
            )
            assert r is None
        pos = pm.get_open_positions()[KEY]
        assert pos.mark_loss_tick_count == 0


# ── Signal-flip detector ────────────────────────────────────────────────


class TestSignalFlip:
    def test_3_consecutive_flip_ticks_exits(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(2):
            r = _check(
                pm,
                surface=make_surface(
                    offset=180,
                    clob_up_bid=0.55,
                    lgb_p_up=0.10,
                    lgb_dist=0.30,
                ),
                flip_enabled=True,
            )
            assert r is None
        reason = _check(
            pm,
            surface=make_surface(
                offset=180,
                clob_up_bid=0.55,
                lgb_p_up=0.10,
                lgb_dist=0.30,
            ),
            flip_enabled=True,
        )
        assert reason is not None and "signal_flip" in reason

    def test_flip_resets_on_non_flip_tick(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(2):
            _check(
                pm,
                surface=make_surface(
                    offset=180,
                    clob_up_bid=0.55,
                    lgb_p_up=0.10,
                    lgb_dist=0.30,
                ),
                flip_enabled=True,
            )
        pos = pm.get_open_positions()[KEY]
        assert pos.flip_consecutive_count == 2

        _check(
            pm,
            surface=make_surface(
                offset=180,
                clob_up_bid=0.55,
                lgb_p_up=0.40,
                lgb_dist=0.20,
            ),
            flip_enabled=True,
        )
        pos = pm.get_open_positions()[KEY]
        assert pos.flip_consecutive_count == 0

    def test_flip_disabled_by_default(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(10):
            r = _check(
                pm,
                surface=make_surface(
                    offset=180,
                    clob_up_bid=0.55,
                    lgb_p_up=0.10,
                    lgb_dist=0.30,
                ),
                flip_enabled=False,
            )
            assert r is None

    def test_flip_outside_offset_window_no_eval(self):
        pm = make_monitor_with_position(direction="UP", fill_price=0.55)
        for _ in range(10):
            r = _check(
                pm,
                surface=make_surface(
                    offset=45,
                    clob_up_bid=0.55,
                    lgb_p_up=0.10,
                    lgb_dist=0.30,
                ),
                flip_enabled=True,
            )
            assert r is None

    def test_flip_for_down_position(self):
        pm = make_monitor_with_position(direction="DOWN", fill_price=0.45)
        for _ in range(2):
            _check(
                pm,
                surface=make_surface(
                    offset=180,
                    clob_down_bid=0.45,
                    lgb_p_up=0.90,
                    lgb_dist=0.30,
                ),
                flip_enabled=True,
            )
        reason = _check(
            pm,
            surface=make_surface(
                offset=180,
                clob_down_bid=0.45,
                lgb_p_up=0.90,
                lgb_dist=0.30,
            ),
            flip_enabled=True,
        )
        assert reason is not None and "lgb_p_up=0.90" in reason
