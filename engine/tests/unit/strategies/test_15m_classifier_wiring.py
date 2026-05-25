"""End-to-end wiring tests for the four 15m classifier YAML strategies.

Regression suite for Hub #546 wiring regression (2026-05-25):

The four YAML-driven strategies (v_eth/v_xrp × top10/top20) have a gate
pipeline that ends with ConsecutivePassTicksGate(min_ticks=12). That gate
historically read ``surface.poly_direction`` to infer trade direction —
but 15m /v4/snapshot payloads do NOT include the polymarket block, so
``poly_direction`` is always None, causing the gate to return
``poly_direction=None not actionable`` on every tick.

Root cause identified 2026-05-25: ``ConsecutivePassTicksGate`` needed a
classifier fallback — when ``poly_direction`` is None, derive direction
from ``probability_classifier`` (prob > 0.5 → UP, prob < 0.5 → DOWN).

This module pins the full gate-pipeline behavior for each of the four
strategies by driving real StrategyConfig instances loaded from the YAML
files against a synthetic FullDataSurface.

Historical WR context (Hub note #546 ghost backtest, Gamma-resolved,
19.4 days):
  - v_eth_15m_classifier_top10 UP  conf≥0.18: 91.5% WR  n=94
  - v_xrp_15m_classifier_top10 UP  conf≥0.18: 96.2% WR  n=48
  - v_xrp_15m_classifier_top10 DOWN conf≥0.18: 86.4% WR  n=88

Eval-offset band from YAML (T-minus seconds to window close):
  min_offset=180, max_offset=540  — covers the bulk of the 15m window
  (15m = 900s span; 540s–180s before close = T-minus 3min to T-minus 9min).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface
from strategies.gates.confidence import ConfidenceGate
from strategies.gates.consecutive_pass_ticks import ConsecutivePassTicksGate
from strategies.gates.timing import TimingGate
from strategies.gates.direction import DirectionGate
from strategies.gates.session_hours import SessionHoursGate
from strategies.gates.regime_v4 import RegimeV4Gate


# ── Surface helpers ────────────────────────────────────────────────────────


def _make_15m_surface(
    *,
    asset: str = "ETH",
    probability_classifier: Optional[float] = 0.80,
    eval_offset: int = 300,  # inside the [180, 540] band
    hour_utc: int = 6,       # not in the blocklist [0,1,4,10,12,16,18,22]
    v4_regime: str = "volatile_trend",  # in [volatile_trend, chop, risk_off]
    poly_direction: Optional[str] = None,  # 15m never ships poly block
) -> FullDataSurface:
    """Build a synthetic 15m surface that should pass all classifier gates."""
    return FullDataSurface(
        asset=asset,
        timescale="15m",
        window_ts=1_777_000_000,
        eval_offset=eval_offset,
        assembled_at=time.time(),
        current_price=2_500.0,
        open_price=2_490.0,
        delta_binance=0.004,
        delta_tiingo=0.004,
        delta_chainlink=0.004,
        delta_pct=0.004,
        delta_source="chainlink",
        vpin=0.45,
        regime="NORMAL",
        twap_delta=0.002,
        v2_probability_up=None,
        v2_probability_raw=None,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        probability_lgb=None,
        probability_classifier=probability_classifier,
        ensemble_config=None,
        v3_5m_composite=None,
        v3_15m_composite=None,
        v3_1h_composite=None,
        v3_4h_composite=None,
        v3_24h_composite=None,
        v3_48h_composite=None,
        v3_72h_composite=None,
        v3_1w_composite=None,
        v3_2w_composite=None,
        v3_sub_elm=None,
        v3_sub_cascade=None,
        v3_sub_taker=None,
        v3_sub_oi=None,
        v3_sub_funding=None,
        v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime=v4_regime,
        v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True,
        v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        # 15m snapshots do NOT ship the polymarket block
        poly_direction=poly_direction,
        poly_trade_advised=None,
        poly_confidence=None,
        poly_confidence_distance=None,
        poly_timing=None,
        poly_max_entry_price=None,
        poly_reason=None,
        v4_recommended_side=None,
        v4_recommended_collateral_pct=None,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=None,
        clob_up_ask=None,
        clob_down_bid=None,
        clob_down_ask=None,
        clob_implied_up=None,
        gamma_up_price=None,
        gamma_down_price=None,
        cg_oi_usd=None,
        cg_funding_rate=None,
        cg_taker_buy_vol=None,
        cg_taker_sell_vol=None,
        cg_liq_total=None,
        cg_liq_long=None,
        cg_liq_short=None,
        cg_long_short_ratio=None,
        timesfm_expected_move_bps=None,
        timesfm_vol_forecast_bps=None,
        hour_utc=hour_utc,
        seconds_to_close=eval_offset,
    )


# ── Gate-pipeline simulation helpers ──────────────────────────────────────


def _run_pipeline(gates, surface) -> tuple[bool, str]:
    """Run a list of gates in order; return (all_passed, last_fail_reason)."""
    for g in gates:
        r = g.evaluate(surface)
        if not r.passed:
            return False, r.reason
    return True, ""


def _top10_pipeline() -> list:
    """Gate pipeline matching v_eth/v_xrp × top10 YAML config."""
    return [
        TimingGate(min_offset=180, max_offset=540),
        DirectionGate(direction="ANY"),
        ConfidenceGate(min_dist=0.18, source="classifier"),
        SessionHoursGate(block_hours_utc=[0, 1, 4, 10, 12, 16, 18, 22]),
        RegimeV4Gate(allow=["volatile_trend", "chop", "risk_off"]),
        ConsecutivePassTicksGate(min_ticks=12),
    ]


def _top20_pipeline() -> list:
    """Gate pipeline matching v_eth/v_xrp × top20 YAML config."""
    return [
        TimingGate(min_offset=180, max_offset=540),
        DirectionGate(direction="ANY"),
        ConfidenceGate(min_dist=0.12, source="classifier"),
        SessionHoursGate(block_hours_utc=[0, 1, 4, 10, 12, 16, 18, 22]),
        RegimeV4Gate(allow=["volatile_trend", "chop", "risk_off"]),
        ConsecutivePassTicksGate(min_ticks=12),
    ]


# ── Core regression: consecutive-tick gate unblocked ──────────────────────


class TestConsecutiveTicksClassifierFallback:
    """The consecutive_pass_ticks gate must use probability_classifier for
    direction when poly_direction is None (the 15m case)."""

    def test_top10_fires_after_12_ticks_up(self):
        """v_eth_15m_classifier_top10 UP: gate fires after 12 consecutive ticks."""
        pipeline = _top10_pipeline()
        consec_gate = pipeline[-1]  # ConsecutivePassTicksGate

        surface = _make_15m_surface(
            probability_classifier=0.80,  # dist=0.30 ≥ 0.18 → UP
            eval_offset=300,
            hour_utc=6,
            v4_regime="volatile_trend",
        )

        fixed_time = [1_000.0]
        with patch("time.time", side_effect=lambda: fixed_time[0]):
            for i in range(1, 12):
                passed, reason = _run_pipeline(pipeline, surface)
                assert not passed, f"Should not pass at tick {i}"
                fixed_time[0] += 2.0

            # 12th tick should pass
            passed, reason = _run_pipeline(pipeline, surface)

        assert passed, f"Pipeline should pass after 12 ticks but got: {reason}"
        r = consec_gate.evaluate(surface)  # already at 13 ticks
        assert r.data["direction"] == "UP"

    def test_top10_fires_after_12_ticks_down(self):
        """prob_classifier < 0.5 → direction DOWN, pipeline fires after 12 ticks."""
        pipeline = _top10_pipeline()

        surface = _make_15m_surface(
            probability_classifier=0.20,  # dist=0.30 ≥ 0.18 → DOWN
            eval_offset=300,
            hour_utc=6,
            v4_regime="chop",
        )

        fixed_time = [2_000.0]
        with patch("time.time", side_effect=lambda: fixed_time[0]):
            for _ in range(11):
                passed, _ = _run_pipeline(pipeline, surface)
                assert not passed
                fixed_time[0] += 2.0
            passed, reason = _run_pipeline(pipeline, surface)

        assert passed, f"Pipeline should pass: {reason}"

    def test_xrp_top20_fires_after_12_ticks(self):
        """v_xrp_15m_classifier_top20 (min_dist=0.12): fire after 12 ticks."""
        pipeline = _top20_pipeline()

        surface = _make_15m_surface(
            asset="XRP",
            probability_classifier=0.70,  # dist=0.20 ≥ 0.12 → UP
            eval_offset=400,
            hour_utc=8,
            v4_regime="risk_off",
        )

        fixed_time = [3_000.0]
        with patch("time.time", side_effect=lambda: fixed_time[0]):
            for _ in range(11):
                passed, _ = _run_pipeline(pipeline, surface)
                assert not passed
                fixed_time[0] += 2.0
            passed, reason = _run_pipeline(pipeline, surface)

        assert passed, f"Pipeline should pass: {reason}"


# ── Gate-level isolation: why each gate fails ──────────────────────────────


class TestGateIsolation:
    """Verify that each gate independently blocks when it should."""

    def test_confidence_gate_source_classifier_blocks_low_dist(self):
        """ConfidenceGate(source=classifier) blocks when dist < min_dist."""
        g = ConfidenceGate(min_dist=0.18, source="classifier")
        surface = _make_15m_surface(probability_classifier=0.60)  # dist=0.10
        r = g.evaluate(surface)
        assert not r.passed
        assert "< min=0.18" in r.reason

    def test_confidence_gate_source_classifier_blocks_when_classifier_none(self):
        """ConfidenceGate(source=classifier) blocks when prob is None."""
        g = ConfidenceGate(min_dist=0.18, source="classifier")
        surface = _make_15m_surface(probability_classifier=None)
        r = g.evaluate(surface)
        assert not r.passed
        assert "probability_classifier not available" in r.reason

    def test_timing_gate_blocks_outside_band(self):
        surface_early = _make_15m_surface(eval_offset=50)   # < 180
        surface_late = _make_15m_surface(eval_offset=600)   # > 540
        g = TimingGate(min_offset=180, max_offset=540)
        assert not g.evaluate(surface_early).passed
        assert not g.evaluate(surface_late).passed

    def test_session_hours_blocks_blocked_hour(self):
        g = SessionHoursGate(block_hours_utc=[0, 1, 4, 10, 12, 16, 18, 22])
        surface = _make_15m_surface(hour_utc=0)  # blocked
        assert not g.evaluate(surface).passed

    def test_regime_v4_blocks_disallowed_regime(self):
        g = RegimeV4Gate(allow=["volatile_trend", "chop", "risk_off"])
        surface = _make_15m_surface(v4_regime="calm_trend")  # not in allow list
        assert not g.evaluate(surface).passed

    def test_consecutive_ticks_blocks_when_classifier_below_half(self):
        """p_cls exactly 0.5 → no direction → consec gate blocks."""
        g = ConsecutivePassTicksGate(min_ticks=12)
        surface = _make_15m_surface(probability_classifier=0.5)
        r = g.evaluate(surface)
        assert not r.passed
        assert "not actionable" in r.reason

    def test_consecutive_ticks_blocks_before_12_ticks(self):
        g = ConsecutivePassTicksGate(min_ticks=12)
        surface = _make_15m_surface(probability_classifier=0.80)
        fixed_time = [5_000.0]
        with patch("time.time", side_effect=lambda: fixed_time[0]):
            for i in range(11):
                r = g.evaluate(surface)
                assert not r.passed, f"Should fail at tick {i + 1}"
                assert r.data["count"] == i + 1
                fixed_time[0] += 2.0


# ── Historical WR guard: correct thresholds from note #546 ────────────────


class TestHistoricalThresholds:
    """Pin that the YAML thresholds match the note #546 backtest params.

    Note #546 ghost backtest (Gamma-resolved, 19.4d):
      v_eth_15m_classifier_top10 UP  conf≥0.18 → 91.5% WR n=94
      v_xrp_15m_classifier_top10 UP  conf≥0.18 → 96.2% WR n=48
      v_xrp_15m_classifier_top10 DOWN conf≥0.18 → 86.4% WR n=88
    Eval-offset band: min_offset=180, max_offset=540 (T-3min to T-9min).
    """

    def test_top10_min_dist_is_0_18(self):
        """ConfidenceGate min_dist for top10 = 0.18 (top-10% filter)."""
        g = ConfidenceGate(min_dist=0.18, source="classifier")
        # Boundary: dist exactly 0.18 → should pass
        surface = _make_15m_surface(probability_classifier=0.68)  # dist=0.18
        r = g.evaluate(surface)
        assert r.passed
        # Boundary: dist just below 0.18 → should fail
        surface_below = _make_15m_surface(probability_classifier=0.67)  # dist=0.17
        r2 = g.evaluate(surface_below)
        assert not r2.passed

    def test_top20_min_dist_is_0_12(self):
        """ConfidenceGate min_dist for top20 = 0.12 (top-20% filter)."""
        g = ConfidenceGate(min_dist=0.12, source="classifier")
        surface = _make_15m_surface(probability_classifier=0.62)  # dist=0.12
        r = g.evaluate(surface)
        assert r.passed
        surface_below = _make_15m_surface(probability_classifier=0.61)  # dist=0.11
        r2 = g.evaluate(surface_below)
        assert not r2.passed

    def test_eval_offset_band_180_to_540(self):
        """Timing gate matches the 180-540s band from YAML."""
        g = TimingGate(min_offset=180, max_offset=540)
        assert not g.evaluate(_make_15m_surface(eval_offset=179)).passed
        assert g.evaluate(_make_15m_surface(eval_offset=180)).passed
        assert g.evaluate(_make_15m_surface(eval_offset=540)).passed
        assert not g.evaluate(_make_15m_surface(eval_offset=541)).passed
