"""Tests for v6_sniper v6.1.2 — Tier-1 rules: LGB alignment + head agreement
+ conf floor.

Data-driven from Hub note #204 / audit #263 (strategy_decisions analysis):
  * DOWN lgb<0.30: 91% WR (n=11) vs DOWN lgb 0.30-0.50: 33% WR — block the bleed
  * UP lgb>0.70: 89% WR — require LGB alignment on UP side too
  * |lgb - path1| < 0.10 = 100% WR (n=8); >= 0.30 degrades to ~71%
  * confidence_score 0.55-0.70: 60% WR; >= 0.90: 100% (n=8)
  * path1_age NOT tightened — all 4 losses were at age<15s, no correlation.

Spec coverage (12 tests):
  1.  test_down_requires_lgb_aligned         — DOWN + lgb>=0.30 → SKIP
  2.  test_down_lgb_aligned_passes           — DOWN + lgb=0.15 → reaches further gates
  3.  test_up_requires_lgb_aligned           — UP + lgb<=0.70 → SKIP
  4.  test_up_lgb_aligned_passes             — UP + lgb=0.85 → reaches further gates
  5.  test_head_agreement_block              — |lgb-path1|>=0.30 → SKIP
  6.  test_head_agreement_passes             — |lgb-path1|<0.30 → reaches further gates
  7.  test_min_confidence_score_block        — confscore<0.70 → SKIP
  8.  test_min_confidence_score_passes       — confscore>=0.70 → reaches further gates
  9.  test_version_bumped                    — YAML version == "6.1.2"
  10. test_risk_off_override_still_false     — regression guard
  11. test_tradeable_is_volatile_only        — regression guard (PR #313 parity)
  12. test_max_offset_sec_is_200             — regression guard (PR #313 parity)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)
V6_YAML = Path(CONFIGS_DIR) / "v6_sniper.yaml"


def _make_down_surface(**overrides) -> FullDataSurface:
    """DOWN-direction surface that PASSES all v6.1.2 Tier-1 gates by default.

    Baseline values:
        lgb = 0.15               (< lgb_aligned_down_max 0.30)
        path1 = 0.18             (|lgb-path1| = 0.03 < 0.30)
        poly_confidence = 0.12   (confscore = 0.76 >= 0.70 floor)
        fill = 0.65 (clob_down_ask)  (above mid-range max 0.60)
    """
    now = time.time()
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=now,
        probability_classifier_inferred_at=now,
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.12, v2_probability_raw=0.12,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.15, probability_classifier=0.18,
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.12,
        poly_confidence_distance=0.38, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.33, clob_up_ask=0.35, clob_down_bid=0.63,
        clob_down_ask=0.65, clob_implied_up=0.34,
        gamma_up_price=0.35, gamma_down_price=0.65,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _make_up_surface(**overrides) -> FullDataSurface:
    """UP-direction surface that PASSES all v6.1.2 Tier-1 gates by default.

    Baseline values:
        lgb = 0.85               (> lgb_aligned_up_min 0.70)
        path1 = 0.88             (|lgb-path1| = 0.03 < 0.30)
        poly_confidence = 0.88   (confscore = 0.76 >= 0.70 floor)
        For UP the strictest bucket gate is up_require_both_buckets, so the
        baseline satisfies BOTH agree_strong AND pegged_path1:
            dist = |0.88 - 0.5| * 2 = 0.76 ... no, dist = 0.38 >= 0.28 ✓
            path1 = 0.88 < 0.95, so NOT pegged — that would fail UP's double-bucket.
        We therefore push path1 to 0.96 (pegged) to keep baseline TRADE-reachable.
    """
    now = time.time()
    up = dict(
        delta_binance=+0.005, delta_tiingo=+0.004, delta_chainlink=+0.005,
        delta_pct=+0.005, delta_source="chainlink",
        v2_probability_up=0.88, v2_probability_raw=0.88,
        probability_lgb=0.85, probability_classifier=0.96,
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.88,
        poly_confidence_distance=0.38,
        v4_macro_bias="BULL", v4_recommended_side="UP",
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.33,
        clob_down_ask=0.35, clob_implied_up=0.66,
        gamma_up_price=0.65, gamma_down_price=0.35,
    )
    up.update(overrides)
    return _make_down_surface(**up)


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v6_sniper", registry.configs["v6_sniper"], surface
    )


def _gate_result(decision, gate_name):
    for g in decision.metadata.get("gate_results", []):
        if g.get("gate") == gate_name:
            return g
    return None


# ── Tier-1 rule #1: LGB alignment ──────────────────────────────────────────
def test_down_requires_lgb_aligned(registry):
    """DOWN + lgb=0.35 (>= lgb_aligned_down_max 0.30) → SKIP.

    The pre-v6.1.2 behaviour only blocked lgb strongly opposing path1 (>
    bucket_lgb_opposite_block). v6.1.2 actively requires lgb < 0.30.
    """
    surface = _make_down_surface(
        probability_lgb=0.35, probability_classifier=0.18,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "lgb_misaligned_down" in (decision.skip_reason or ""), decision.skip_reason
    g = _gate_result(decision, "lgb_aligned")
    assert g is not None and g["passed"] is False


def test_down_lgb_aligned_passes(registry):
    """DOWN + lgb=0.15 + path1=0.18 → lgb_aligned gate PASSES.

    Downstream gates still apply but we only pin lgb_aligned here.
    """
    surface = _make_down_surface(
        probability_lgb=0.15, probability_classifier=0.18,
    )
    decision = _evaluate(registry, surface)
    # Whichever way the rest of the pipeline goes, the lgb_aligned gate must
    # have been evaluated and passed.
    g = _gate_result(decision, "lgb_aligned")
    assert g is not None, f"lgb_aligned gate missing; gates={decision.metadata.get('gate_results')}"
    assert g["passed"] is True, g


def test_up_requires_lgb_aligned(registry):
    """UP + lgb=0.65 (<= lgb_aligned_up_min 0.70) → SKIP."""
    surface = _make_up_surface(
        probability_lgb=0.65, probability_classifier=0.96,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "lgb_misaligned_up" in (decision.skip_reason or ""), decision.skip_reason
    g = _gate_result(decision, "lgb_aligned")
    assert g is not None and g["passed"] is False


def test_up_lgb_aligned_passes(registry):
    """UP + lgb=0.85 + path1=0.98 → lgb_aligned passes."""
    surface = _make_up_surface(
        probability_lgb=0.85, probability_classifier=0.98,
        poly_confidence=0.90, poly_confidence_distance=0.40,
    )
    decision = _evaluate(registry, surface)
    g = _gate_result(decision, "lgb_aligned")
    assert g is not None, f"lgb_aligned gate missing; gates={decision.metadata.get('gate_results')}"
    assert g["passed"] is True, g


# ── Tier-1 rule #2: Head agreement ─────────────────────────────────────────
def test_head_agreement_block(registry):
    """DOWN + lgb=0.10 + path1=0.50 → |lgb-path1|=0.40 >= 0.30 → SKIP."""
    surface = _make_down_surface(
        probability_lgb=0.10, probability_classifier=0.50,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "heads_disagree" in (decision.skip_reason or ""), decision.skip_reason
    g = _gate_result(decision, "head_agreement")
    assert g is not None and g["passed"] is False


def test_head_agreement_passes(registry):
    """DOWN + lgb=0.10 + path1=0.05 → |lgb-path1|=0.05 < 0.30 → passes."""
    surface = _make_down_surface(
        probability_lgb=0.10, probability_classifier=0.05,
        poly_confidence=0.08, poly_confidence_distance=0.42,
    )
    decision = _evaluate(registry, surface)
    g = _gate_result(decision, "head_agreement")
    assert g is not None, f"head_agreement gate missing; gates={decision.metadata.get('gate_results')}"
    assert g["passed"] is True, g


# ── Tier-1 rule #3: Min confidence_score floor ─────────────────────────────
def test_min_confidence_score_block(registry):
    """probability_used=0.62 → confscore=|0.62-0.5|*2=0.24 < 0.70 floor → SKIP.

    Note: we must route past the bucket-classification stage — dist=0.12
    would classify as mid_conf and SKIP there first. So we use a pegged
    surface (path1 extreme) which takes precedence over agree_strong and
    lets the flow reach the min_confidence_score gate.
    """
    surface = _make_down_surface(
        poly_confidence=0.38, poly_confidence_distance=0.12,
        probability_lgb=0.25, probability_classifier=0.05,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # Must SKIP on the conf_score gate, not an earlier gate.
    assert "conf_score_below_floor" in (decision.skip_reason or ""), decision.skip_reason
    g = _gate_result(decision, "min_confidence_score")
    assert g is not None and g["passed"] is False


def test_min_confidence_score_passes(registry):
    """probability_used=0.85 → confscore=0.70 >= 0.70 floor → passes.

    DOWN direction: probability_used=0.15 → confscore=|0.15-0.5|*2=0.70.
    """
    surface = _make_down_surface(
        poly_confidence=0.15, poly_confidence_distance=0.35,
        probability_lgb=0.18, probability_classifier=0.15,
    )
    decision = _evaluate(registry, surface)
    g = _gate_result(decision, "min_confidence_score")
    assert g is not None, f"min_confidence_score gate missing; gates={decision.metadata.get('gate_results')}"
    assert g["passed"] is True, g


# ── Regression guards ──────────────────────────────────────────────────────
def test_version_bumped():
    """YAML version must be exactly 6.1.2."""
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["version"] == "6.1.2", f"expected 6.1.2, got {data['version']}"


def test_risk_off_override_still_false():
    """Regression guard: risk_off_override_enabled must stay False (Montreal hotfix)."""
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["risk_off_override_enabled"] is False, (
        f"risk_off_override_enabled must stay False, "
        f"got {data['gate_params']['risk_off_override_enabled']}"
    )


def test_tradeable_is_volatile_only():
    """Regression guard (PR #313 parity): regime restricted to volatile_trend."""
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["tradeable_v4_regimes"] == ["volatile_trend"], (
        f"tradeable_v4_regimes must be exactly ['volatile_trend'], "
        f"got {data['gate_params']['tradeable_v4_regimes']}"
    )


def test_max_offset_sec_is_200():
    """Regression guard (PR #313 parity): max_offset_sec pinned to 200."""
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["max_offset_sec"] == 200, (
        f"max_offset_sec must be 200, got {data['gate_params']['max_offset_sec']}"
    )
