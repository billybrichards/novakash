"""Tests for v4_down_only v2.3.0 — ensemble-era guards + tight conviction.

Data-driven from Hub note #198:
  * First LIVE trade today (v2.2.0) = LOSS at MEDIUM conv, dist=0.10,
    volatile_trend, -$3.68.
  * v5_ensemble lifetime MEDIUM bucket: 46% WR / -$3.67 net.

v2.3.0 tightens the entry via four new declarative gates:
  - confidence min_dist 0.10 → 0.18
  - conviction_gate allowed=[HIGH]
  - vpin_gate min=0.40, block_cascade=true
  - source_agreement null-block chainlink + tiingo (min_sources=0)
  - regime_v4 allow=[calm_trend, volatile_trend]
  - entry_cap_override 0.90 → 0.85

8 test cases:
  1. medium_conviction_blocked
  2. high_conviction_allowed
  3. vpin_cascade_blocked
  4. source_null_blocked
  5. chop_regime_blocked
  6. risk_off_regime_blocked
  7. entry_cap_085
  8. version_bumped
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
V4_YAML = Path(CONFIGS_DIR) / "v4_down_only.yaml"


def _make_surface(**overrides) -> FullDataSurface:
    """Base surface: v4_down_only v2.3.0 accept-path (DOWN, HIGH, in-range,
    both oracles present, calm_trend regime, VPIN above floor, not CASCADE).
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.25, v2_probability_raw=0.25,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=0.25,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.25,
        poly_confidence_distance=0.25, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.38, clob_up_ask=0.40, clob_down_bid=0.58,
        clob_down_ask=0.60, clob_implied_up=0.39,
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


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v4_down_only", registry.configs["v4_down_only"], surface
    )


# ── Test 1 ──────────────────────────────────────────────────────────────────
def test_medium_conviction_blocked(registry):
    """conviction=MEDIUM + dist=0.15 (below 0.18 floor) → SKIP."""
    # Confidence gate (0.18) blocks first, then conviction_gate would
    # also block. We check the whole skip chain catches it regardless.
    surface = _make_surface(
        v4_conviction="MEDIUM", poly_confidence=0.35,
        poly_confidence_distance=0.15,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # Either confidence or conviction gate should fire first; both count as pass.
    assert (
        "confidence" in (decision.skip_reason or "")
        or "conviction_gate" in (decision.skip_reason or "")
    ), f"unexpected skip_reason: {decision.skip_reason}"


# ── Test 2 ──────────────────────────────────────────────────────────────────
def test_high_conviction_allowed(registry):
    """conviction=HIGH + dist=0.22 (above 0.18 floor) + all other gates
    pass → TRADE."""
    surface = _make_surface(
        v4_conviction="HIGH", poly_confidence=0.28,
        poly_confidence_distance=0.22,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert decision.direction == "DOWN"


# ── Test 3 ──────────────────────────────────────────────────────────────────
def test_vpin_cascade_blocked(registry):
    """vpin=0.80 + vpin_regime=CASCADE → SKIP via vpin_gate."""
    surface = _make_surface(vpin=0.80, regime="CASCADE")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "vpin_gate" in (decision.skip_reason or ""), (
        f"expected vpin_gate in skip_reason, got: {decision.skip_reason}"
    )
    assert "CASCADE" in (decision.skip_reason or "")


# ── Test 4 ──────────────────────────────────────────────────────────────────
def test_source_null_blocked(registry):
    """delta_chainlink=None → SKIP via source_agreement null-block."""
    surface = _make_surface(delta_chainlink=None)
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "source_agreement" in (decision.skip_reason or "")
    assert "chainlink" in (decision.skip_reason or "").lower()


# ── Test 5 ──────────────────────────────────────────────────────────────────
def test_chop_regime_blocked(registry):
    """regime=chop → SKIP via regime_v4 (only calm_trend + volatile_trend allowed)."""
    surface = _make_surface(v4_regime="chop")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "regime_v4" in (decision.skip_reason or "")
    assert "chop" in (decision.skip_reason or "")


# ── Test 6 ──────────────────────────────────────────────────────────────────
def test_risk_off_regime_blocked(registry):
    """regime=risk_off → SKIP (DOWN-only is strict, no risk_off override)."""
    surface = _make_surface(v4_regime="risk_off")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "regime_v4" in (decision.skip_reason or "")
    assert "risk_off" in (decision.skip_reason or "")


# ── Test 7 ──────────────────────────────────────────────────────────────────
def test_entry_cap_085():
    """YAML gate_params.entry_cap_override must be 0.85."""
    data = yaml.safe_load(V4_YAML.read_text())
    assert data["gate_params"]["entry_cap_override"] == pytest.approx(0.85), (
        f"expected 0.85, got {data['gate_params']['entry_cap_override']}"
    )


# ── Test 8 ──────────────────────────────────────────────────────────────────
def test_version_bumped():
    """YAML version must be 2.3.0."""
    data = yaml.safe_load(V4_YAML.read_text())
    assert data["version"] == "2.3.0", f"expected 2.3.0, got {data['version']}"


# ── Test 9 ──────────────────────────────────────────────────────────────────
def test_mode_is_ghost():
    """YAML mode must be GHOST — pins the manual Montreal flip so that
    merging this PR does not regress prod back to LIVE. See Hub note #198
    + audit #260. Flip back to LIVE only after SOT fix + v2.3.0 validation.
    """
    data = yaml.safe_load(V4_YAML.read_text())
    assert data["mode"] == "GHOST", f"expected GHOST, got {data['mode']}"
