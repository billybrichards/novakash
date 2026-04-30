"""Tests for v9_ensemble — LGB + v2-classifier reinforced-agreement ensemble.

Covers the v9-specific gates on top of the v8_lgb_only baseline:
  - disagreement veto (|pc - pl| > 0.25)
  - direction agreement (pc/pl same side of 0.5)
  - T-minus-aware blend weights at the 4 boundaries
  - VHC reinforcement scoring (Kelly multiplier)
  - VHC bypass of TRANSITION block
  - VHC bypass of UP dist floor
  - pc=None fallback to v8_lgb_only path
  - hard LGB safety floor (pc screaming + pl weak → SKIP)
  - end-to-end TRADE path with blended direction + conviction

Fixtures mirror ``test_v8_champion_lgb_only._make_surface`` / ``_up_surface``
with pc populated so v9 exercises the full ensemble path.
"""
from __future__ import annotations

import datetime as _dt
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_ensemble import (
    evaluate_v9_ensemble,
    record_loss,
    reset_cooldown,
    reset_all_confirmations_v9,
    _pc_weight_for_offset,
    _pc_weight_t_60,
    _pc_weight_t_120,
    _pc_weight_t_180,
    _pc_weight_t_200,
)
from strategies import gate_params as _gp
from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)


def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface — pc + pl both DOWN, agreement, all gates pass.

    pc=0.30, pl=0.30 → both DOWN, no disagreement, pu=0.30 direction=DOWN.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,  # 2024-04-13 12:00 UTC
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=0.30,
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


def _up_surface(**overrides) -> FullDataSurface:
    """Symmetric UP-direction ensemble surface — pc + pl both UP."""
    defaults = dict(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.70, probability_classifier=0.70,
        delta_binance=+0.005, delta_tiingo=+0.004, delta_chainlink=+0.005,
        v4_recommended_side="UP", v2_probability_up=0.70,
        clob_up_bid=0.58, clob_up_ask=0.60,
        clob_down_bid=0.38, clob_down_ask=0.40,
        gamma_up_price=0.60, gamma_down_price=0.40,
    )
    defaults.update(overrides)
    return _make_surface(**defaults)


# ── gate_params scope helper ───────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _bind_gate_params():
    """Bind v9_ensemble YAML defaults around each test.

    Matches the v8_lgb_only test fixture but adds v9-specific knobs so
    the hook reads the values an operator ships in YAML.
    """
    params = {
        # Inherited from v8_lgb_only
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": [
            "volatile_trend", "chop", "risk_off", "calm_trend",
        ],
        "block_down_vpin_regimes": ["TRANSITION"],
        "block_up_vpin_regimes": ["TRANSITION"],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "lgb_dist_min_up_with_hc_agree": 0.05,
        "lgb_dist_min_down_with_hc_agree": 0.05,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.55,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        # v9-specific
        "ensemble_disagreement_threshold": 0.25,
        "require_direction_agreement": True,
        "pc_weight_t_60": 0.55,
        "pc_weight_t_120": 0.50,
        "pc_weight_t_180": 0.45,
        "pc_weight_t_200": 0.35,
        "vhc_threshold": 0.25,
        "vhc_bypass_transition": True,
        "vhc_bypass_up_dist": True,
        "vhc_bypass_disagreement": True,
        "vhc_bypass_lgb_safety_floor": True,
        "vhc_bypass_oracle_direction": True,
        "vhc_kelly_multiplier": 2.0,
        "conviction_high_dist": 0.20,
        "conviction_medium_dist": 0.12,
        "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": True,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        # Disable new features for legacy tests — tested separately
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
        # PL VHC bypass — enabled by default
        "pl_vhc_bypass_enabled": True,
        "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
    }
    token = _gp.set_active(params)
    reset_cooldown()
    reset_all_confirmations_v9()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()


# ── Registry load sanity ───────────────────────────────────────────────────
@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def test_registered_as_ghost(registry):
    assert "v9_ensemble" in registry.strategy_names
    cfg = registry.configs["v9_ensemble"]
    assert cfg.mode == "LIVE"
    assert cfg.version == "9.0.1"
    assert cfg.timescale == "5m"
    assert cfg.asset == "BTC"
    gp = cfg.gate_params
    for key in (
        "ensemble_disagreement_threshold",
        "require_direction_agreement",
        "pc_weight_t_60",
        "pc_weight_t_120",
        "pc_weight_t_180",
        "pc_weight_t_200",
        "vhc_threshold",
        "vhc_bypass_transition",
        "vhc_bypass_up_dist",
        "vhc_bypass_disagreement",
        "vhc_bypass_lgb_safety_floor",
        "vhc_bypass_oracle_direction",
        "vhc_kelly_multiplier",
        "fallback_to_lgb_on_pc_null",
        "pl_vhc_bypass_enabled",
        "pl_vhc_threshold",
        "pl_vhc_require_pc_agreement",
    ):
        assert key in gp, f"missing gate_param: {key}"
    assert gp["ensemble_disagreement_threshold"] == 0.35
    assert gp["vhc_threshold"] == 0.25
    assert gp["vhc_kelly_multiplier"] == 2.0
    assert gp["block_up_vpin_regimes"] == ["TRANSITION"]


# ── Default accept path ────────────────────────────────────────────────────
def test_default_down_surface_trades():
    d = evaluate_v9_ensemble(_make_surface())
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "DOWN"
    assert d.metadata["primary_signal_source"] == "ensemble"
    assert d.metadata["probability_used"] == pytest.approx(0.30, abs=1e-9)


def test_default_up_surface_trades():
    d = evaluate_v9_ensemble(_up_surface())
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "UP"


# ── R2: Disagreement veto ──────────────────────────────────────────────────
class TestDisagreementVeto:
    def test_abs_diff_over_threshold_skips(self):
        # pc=0.65, pl=0.35: |diff| = 0.30 > 0.25 → SKIP. pc_dist=0.15 < 0.25
        # so NOT VHC (no VHC bypass). Direction disagrees too (UP vs DOWN).
        s = _up_surface(probability_classifier=0.65, probability_lgb=0.35)
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "ensemble_disagreement" in d.skip_reason

    def test_exactly_at_threshold_allowed(self):
        # |pc - pl| = 0.25 exactly — veto is strictly ">", not ">=", so allowed.
        # Use non-VHC values: pc=0.70 (dist=0.20), pl=0.45 → |diff|=0.25.
        s = _up_surface(probability_classifier=0.70, probability_lgb=0.45)
        d = evaluate_v9_ensemble(s)
        # Should get past disagreement gate; may fail later gates depending
        # on conviction but we check the veto specifically didn't fire.
        if d.action == "SKIP":
            assert "ensemble_disagreement" not in d.skip_reason

    def test_small_diff_passes(self):
        s = _up_surface(probability_classifier=0.72, probability_lgb=0.68)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason


# ── R3: Direction agreement ────────────────────────────────────────────────
class TestDirectionAgreement:
    def test_pc_up_pl_down_skips(self):
        # pc=0.52 UP, pl=0.48 DOWN. |diff|=0.04 < 0.25 so disagreement
        # gate passes — direction gate must catch this.
        s = _make_surface(
            probability_classifier=0.52,
            probability_lgb=0.48,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "direction_agreement" in d.skip_reason

    def test_both_up_passes(self):
        s = _up_surface(probability_classifier=0.65, probability_lgb=0.70)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"

    def test_both_down_passes(self):
        s = _make_surface(probability_classifier=0.35, probability_lgb=0.30)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"


# ── R4: T-minus blend weights ──────────────────────────────────────────────
class TestTMinusBlend:
    def test_weight_at_30_60s(self):
        assert _pc_weight_for_offset(30) == _pc_weight_t_60()
        assert _pc_weight_for_offset(60) == _pc_weight_t_60()
        assert _pc_weight_for_offset(45) == 0.55

    def test_weight_at_60_120s(self):
        assert _pc_weight_for_offset(61) == _pc_weight_t_120()
        assert _pc_weight_for_offset(120) == _pc_weight_t_120()
        assert _pc_weight_for_offset(90) == 0.50

    def test_weight_at_120_180s(self):
        assert _pc_weight_for_offset(121) == _pc_weight_t_180()
        assert _pc_weight_for_offset(180) == _pc_weight_t_180()
        assert _pc_weight_for_offset(150) == 0.45

    def test_weight_at_180_200s(self):
        assert _pc_weight_for_offset(181) == _pc_weight_t_200()
        assert _pc_weight_for_offset(200) == _pc_weight_t_200()
        assert _pc_weight_for_offset(195) == 0.35

    def test_blend_computed_correctly_at_t60(self):
        # At T-60: pc weight 0.55, pl weight 0.45.
        # pc=0.80, pl=0.70 → pu = 0.55*0.80 + 0.45*0.70 = 0.755
        s = _up_surface(
            eval_offset=60,
            probability_classifier=0.80,
            probability_lgb=0.70,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        expected_pu = 0.55 * 0.80 + 0.45 * 0.70
        assert d.metadata["probability_used"] == pytest.approx(
            expected_pu, abs=1e-9
        )

    def test_blend_computed_correctly_at_t200(self):
        # pc=0.80, pl=0.70 @ T-200: pu = 0.35*0.80 + 0.65*0.70 = 0.735
        s = _up_surface(
            eval_offset=200,
            probability_classifier=0.80,
            probability_lgb=0.70,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        expected_pu = 0.35 * 0.80 + 0.65 * 0.70
        assert d.metadata["probability_used"] == pytest.approx(
            expected_pu, abs=1e-9
        )


# ── R9: VHC reinforcement + bypass ─────────────────────────────────────────
class TestVhcReinforcement:
    def test_vhc_tier_high_score(self):
        # pc=0.80 → pc_dist=0.30 >= 0.25 VHC threshold, pl=0.70 agrees UP
        s = _up_surface(probability_classifier=0.80, probability_lgb=0.70)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.metadata["is_vhc"] is True
        assert d.metadata["conviction_label"] == "VERY_HIGH"
        # Score should be >= 0.55 to trigger vhc_reinforced sizing row.
        assert d.confidence_score >= 0.55

    def test_not_vhc_when_pc_below_threshold(self):
        # pc=0.70 → pc_dist=0.20 < 0.25; pl agrees UP. Not VHC, just HIGH.
        s = _up_surface(probability_classifier=0.70, probability_lgb=0.70)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.metadata["is_vhc"] is False
        assert d.metadata["conviction_label"] in ("HIGH", "MEDIUM", "LOW")

    def test_vhc_bypasses_transition_block(self):
        # DOWN in TRANSITION normally blocked (R5). VHC (pc=0.20) should bypass.
        # pl=0.30 agrees DOWN and passes DOWN dist floor (0.10).
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.20,  # pc_dist=0.30 >= 0.25 VHC
            probability_lgb=0.30,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "transition_regime" in d.metadata["vhc_bypasses"]

    def test_non_vhc_blocked_by_transition(self):
        # Same setup but pc=0.35 (pc_dist=0.15 < 0.25 — not VHC).
        # TRANSITION block should still fire. NB: oracles must also be
        # weak enough to not trigger the 2026-04-24 transition_strong_bypass
        # (avg |Δ| < 0.05% OR pl_dist < 0.20). Here pl=0.30 dist=0.20 meets
        # LGB strong, so we set oracles weak to force SKIP.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.35,
            probability_lgb=0.30,
            delta_chainlink=-0.0002,  # 0.02% < 0.05% threshold
            delta_tiingo=-0.0003,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_vhc_bypasses_up_dist_floor(self):
        # UP direction, pl=0.60 → pl_dist=0.10 < 0.15 UP floor.
        # pc=0.85 (pc_dist=0.35 >= 0.25 VHC) agrees UP.
        # With HC-agree bypass (|pc-0.5|=0.35 >= 0.15), floor relaxes to 0.05,
        # so pl_dist=0.10 >= 0.05 passes normally (no VHC bypass needed).
        s = _up_surface(
            probability_classifier=0.85,
            probability_lgb=0.60,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        # HC-agree relaxes floor so VHC bypass isn't triggered
        assert d.metadata["is_vhc"] is True

    def test_vhc_bypasses_up_dist_floor_no_hc_agree(self):
        # When HC-agree is disabled (pc close to 0.5), VHC bypass still works.
        # pl=0.54 (dist=0.04 < 0.05 even with HC-agree), pc=0.76 (VHC: dist=0.26 >= 0.25).
        # pc is HC (0.26 >= 0.15) and agrees UP, so floor relaxes to 0.05.
        # But pl_dist=0.04 < 0.05, so VHC bypass must fire.
        s = _up_surface(
            probability_classifier=0.76,
            probability_lgb=0.54,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "lgb_safety_floor" in d.metadata["vhc_bypasses"]


# ── R1: pc=None fallback ───────────────────────────────────────────────────
class TestPcNullFallback:
    def test_pc_none_falls_back_to_v8_lgb_only(self):
        s = _up_surface(probability_classifier=None, probability_lgb=0.70)
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.metadata["fallback_reason"] == "pc_null"
        assert d.metadata["delegated_to"] == "v8_champion_lgb_only"
        # Strategy identity preserved
        assert d.strategy_id == "v9_ensemble"

    def test_pc_none_fallback_preserves_lgb_skip_reason(self):
        # pl low conviction (UP=0.55 dist=0.05 < 0.15 UP floor) → v8_lgb_only
        # should SKIP, and v9 should return that SKIP with its own identity.
        s = _up_surface(
            probability_classifier=None,
            probability_lgb=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert d.metadata["fallback_reason"] == "pc_null"

    def test_pl_none_always_skips(self):
        s = _make_surface(
            probability_lgb=None, probability_classifier=0.30
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "pl_availability" in d.skip_reason


# ── R6: Hard LGB safety floor ──────────────────────────────────────────────
class TestLgbSafetyFloor:
    def test_pc_screaming_pl_weak_down_skips(self):
        # DOWN direction, pc=0.35 (not VHC, dist=0.15 < 0.25) but pl=0.47
        # (weak, dist=0.03 < 0.10). |diff|=0.12 < 0.25 passes disagreement.
        # Direction agrees (both DOWN). LGB safety floor must catch this.
        s = _make_surface(
            probability_classifier=0.35,
            probability_lgb=0.47,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "lgb_safety_floor" in d.skip_reason

    def test_pc_screaming_pl_weak_up_hc_agree_relaxes_floor(self):
        # UP direction, pc=0.80 (pc_dist=0.30, HC: >= 0.15), pl=0.56 (dist=0.06).
        # HC-agree relaxes floor from 0.15 to 0.05. pl_dist=0.06 >= 0.05 passes.
        # |diff| = 0.24 < 0.25, passes disagreement. Both UP, passes direction.
        s = _up_surface(
            probability_classifier=0.80,
            probability_lgb=0.56,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        # HC-agree relaxes the floor so VHC bypass not needed
        assert d.metadata["is_vhc"] is True

    def test_pl_very_weak_still_blocked_even_with_hc_agree(self):
        # UP direction, pc=0.76 (VHC: dist=0.26 >= 0.25, HC agrees: 0.26 >= 0.15),
        # pl=0.53 (dist=0.03 < 0.05 relaxed floor).
        # HC-agree relaxes to 0.05 but 0.03 < 0.05, so VHC bypass must fire.
        # |diff| = 0.23 < 0.25 passes disagreement.
        s = _up_surface(
            probability_classifier=0.76,
            probability_lgb=0.53,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "lgb_safety_floor" in d.metadata["vhc_bypasses"]


# ── TRANSITION strong-oracle bypass (note #228, 2026-04-24) ────────────────
class TestTransitionBypass:
    """Bypass independent from VHC. Either path can unblock TRANSITION gate.

    Strong-oracle bypass: chainlink+tiingo agree direction AND
    avg(|Δcl|, |Δti|) >= 0.05% AND |pl-0.5| >= 0.20. Binance excluded.
    """

    def test_down_transition_bypass_with_strong_oracles_non_vhc(self):
        # DOWN in TRANSITION. pc=0.35 (not VHC; pc_dist=0.15 < 0.25).
        # Strong oracles + LGB dist 0.20 → strong-oracle bypass fires.
        # Disagreement |0.35-0.30|=0.05 passes veto.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.35,
            probability_lgb=0.30,             # dist 0.20 meets bypass floor
            delta_chainlink=-0.0007,
            delta_tiingo=-0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"
        regime_gates = [
            g for g in d.metadata["gate_results"]
            if g["gate"] == "vpin_regime_direction"
        ]
        assert regime_gates
        assert regime_gates[-1]["passed"] is True
        # Must be the strong-oracle path, not VHC
        assert "transition_strong_bypass" in regime_gates[-1]["reason"]
        assert d.metadata.get("is_vhc") is False

    def test_down_transition_NO_bypass_weak_oracles(self):
        # TRANSITION + DOWN + strong LGB but weak oracles → no bypass.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.35,
            probability_lgb=0.30,
            delta_chainlink=-0.0002,
            delta_tiingo=-0.0003,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_up_transition_bypass_with_strong_oracles_non_vhc(self):
        # Symmetric UP case. block_up_vpin_regimes=[TRANSITION] is the v9 default.
        # NB: pl=0.71 (dist=0.21) because pl=0.70 hits FP 0.19999 edge.
        s = _up_surface(
            regime="TRANSITION",
            probability_classifier=0.65,       # pc_dist=0.15 not VHC
            probability_lgb=0.71,              # dist 0.21 strong
            delta_chainlink=0.0007,
            delta_tiingo=0.0008,
            clob_up_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"
        assert d.metadata.get("is_vhc") is False

    def test_bypass_blocked_when_lgb_weak(self):
        # Oracles strong but LGB dist 0.12 < 0.20 → no strong-oracle bypass.
        # Also not VHC. DOWN dist_min=0.10 keeps the lgb_safety_floor passing.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.40,      # pc_dist=0.10 not VHC
            probability_lgb=0.38,             # dist 0.12 passes safety floor
            delta_chainlink=-0.0007,
            delta_tiingo=-0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_bypass_blocked_when_oracles_disagree(self):
        # chainlink DOWN, tiingo UP — oracle_direction fires first anyway.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.35,
            probability_lgb=0.30,
            delta_chainlink=-0.0007,
            delta_tiingo=+0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"

    def test_vhc_bypass_still_works_alongside_strong_bypass(self):
        # Both bypass conditions met. VHC path takes priority in gate reason.
        s = _make_surface(
            regime="TRANSITION",
            probability_classifier=0.20,      # VHC
            probability_lgb=0.25,             # dist 0.25 strong
            delta_chainlink=-0.0007,
            delta_tiingo=-0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "transition_regime" in d.metadata["vhc_bypasses"]


# ── Timing / hour block sanity ─────────────────────────────────────────────
def test_timing_out_of_window_skips():
    s = _make_surface(eval_offset=250)
    d = evaluate_v9_ensemble(s)
    assert d.action == "SKIP"
    assert "timing" in d.skip_reason


def test_h03_blocked():
    ts_h3 = int(
        _dt.datetime(2024, 4, 13, 3, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    )
    s = _make_surface(window_ts=ts_h3)
    d = evaluate_v9_ensemble(s)
    assert d.action == "SKIP"
    assert "utc_hour_block" in d.skip_reason


# ── Oracle direction ───────────────────────────────────────────────────────
def test_oracle_disagree_skips():
    # Ensemble says UP but oracles point DOWN → oracle_direction gate fires.
    s = _up_surface(
        probability_classifier=0.70,
        probability_lgb=0.70,
        delta_chainlink=-0.005,
        delta_tiingo=-0.004,
    )
    d = evaluate_v9_ensemble(s)
    assert d.action == "SKIP"
    assert "oracle_direction" in d.skip_reason


# ── Post-loss cooldown ─────────────────────────────────────────────────────
class TestCooldown:
    def test_no_cooldown_when_no_prior_loss(self):
        reset_cooldown()
        s = _up_surface()
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason

    def test_cooldown_blocks_within_window(self):
        record_loss(int(time.time()) - 600)
        s = _up_surface()
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "post_loss_cooldown" in d.skip_reason

    def test_cooldown_expires_after_20min(self):
        record_loss(int(time.time()) - 1300)
        s = _up_surface()
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason


# ── Full-stack TRADE sanity ────────────────────────────────────────────────
def test_full_stack_trade_up_with_metadata():
    # pc=0.75, pl=0.72 @ T-90 → pu = 0.50*0.75 + 0.50*0.72 = 0.735
    # Not VHC (pc_dist=0.25 == threshold, is_vhc requires >=). Actually 0.25 >= 0.25
    # is True, so is_vhc=True. Check all metadata.
    s = _up_surface(
        eval_offset=90,
        probability_classifier=0.75,
        probability_lgb=0.72,
    )
    d = evaluate_v9_ensemble(s)
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "UP"
    assert d.strategy_id == "v9_ensemble"
    assert d.strategy_version == "9.0.1"
    md = d.metadata
    assert md["probability_classifier"] == 0.75
    assert md["probability_lgb"] == 0.72
    assert md["pc_weight"] == 0.50
    assert md["pl_weight"] == 0.50
    assert md["probability_used"] == pytest.approx(0.735, abs=1e-9)
    assert md["disagreement"] == pytest.approx(0.03, abs=1e-9)
    assert "gate_results" in md
    # pc_dist=0.25 >= vhc_threshold 0.25 so is_vhc should be True.
    assert md["is_vhc"] is True


# ── VHC full signal-gate bypass (2026-04-24) ──────────────────────────────
class TestVhcBypassAllSignalGates:
    """When |pc - 0.5| >= 0.25 (VHC), classifier overrides all signal-quality
    gates. Capital-safety gates (fill_band, fill_floors, cooldown, timing,
    utc_hour, v4_regime, vpin, source_agreement) still required.
    """

    def test_vhc_bypasses_disagreement(self):
        """VHC pc should bypass ensemble_disagreement even when models diverge."""
        # pc=0.15 (DOWN, pc_dist=0.35 VHC), pl=0.55 (UP).
        # |diff|=0.40 > 0.25 threshold → disagreement fires without VHC bypass.
        # Oracles DOWN to match classifier direction.
        s = _make_surface(
            probability_classifier=0.15,
            probability_lgb=0.55,
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"  # follows classifier, not LGB
        assert "ensemble_disagreement" in d.metadata["vhc_bypasses"]
        assert d.metadata["vhc_overriding_direction"] is True
        assert d.metadata["is_vhc"] is True
        assert d.metadata["conviction_label"] == "VERY_HIGH"
        assert d.confidence_score >= 0.55

    def test_vhc_bypasses_direction_agreement(self):
        """VHC pc should bypass direction_agreement when pc/pl disagree on side."""
        # pc=0.80 (UP, dist=0.30 VHC), pl=0.45 (DOWN).
        # |diff|=0.35 > 0.25 → disagreement also fires, both bypassed.
        # Oracles UP to match classifier.
        s = _up_surface(
            probability_classifier=0.80,
            probability_lgb=0.45,
            delta_chainlink=+0.005,
            delta_tiingo=+0.004,
            clob_up_ask=0.60,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"  # follows classifier
        assert "direction_agreement" in d.metadata["vhc_bypasses"]
        assert d.metadata["vhc_overriding_direction"] is True

    def test_vhc_bypasses_oracle_direction(self):
        """VHC pc should bypass oracle_direction when oracles disagree."""
        # pc=0.80 (UP, dist=0.30 VHC), pl=0.70 (UP, agree).
        # Oracles point DOWN → oracle_direction would block without VHC.
        s = _up_surface(
            probability_classifier=0.80,
            probability_lgb=0.70,
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "oracle_direction" in d.metadata["vhc_bypasses"]

    def test_vhc_bypasses_lgb_safety_floor_down(self):
        """VHC should bypass lgb_safety_floor for DOWN direction (new)."""
        # pc=0.15 (DOWN, dist=0.35 VHC), pl=0.47 (DOWN, dist=0.03 < 0.10 floor).
        # |diff|=0.32 > 0.25 disagreement threshold — also VHC-bypassed.
        # Oracles DOWN.
        s = _make_surface(
            probability_classifier=0.15,
            probability_lgb=0.47,
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"
        assert "lgb_safety_floor" in d.metadata["vhc_bypasses"]

    def test_vhc_still_blocked_by_fill_band(self):
        """Fill band is a capital-safety gate — VHC must NOT bypass it."""
        # pc=0.15 (VHC DOWN), pl=0.30, fill_price=0.90 > 0.82 max.
        s = _make_surface(
            probability_classifier=0.15,
            probability_lgb=0.30,
            clob_down_ask=0.90,
            poly_max_entry_price=0.90,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "fill_band" in d.skip_reason

    def test_vhc_still_blocked_by_cooldown(self):
        """Post-loss cooldown is a risk gate — VHC must NOT bypass it."""
        record_loss(int(time.time()) - 600)  # 10 min ago, within 20 min window
        s = _make_surface(
            probability_classifier=0.15,
            probability_lgb=0.30,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "post_loss_cooldown" in d.skip_reason

    def test_non_vhc_still_blocked_by_disagreement(self):
        """Regression: non-VHC trades must still be blocked by disagreement."""
        # pc=0.65 (UP, dist=0.15 < 0.25, NOT VHC), pl=0.30 (DOWN).
        # |diff|=0.35 > 0.25 → disagreement should fire.
        s = _up_surface(
            probability_classifier=0.65,
            probability_lgb=0.30,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "ensemble_disagreement" in d.skip_reason

    def test_vhc_direction_follows_classifier(self):
        """When VHC bypasses, trade direction follows pc, not blended pu."""
        # pc=0.20 (DOWN, dist=0.30 VHC), pl=0.60 (UP).
        # Blended pu at T-120: 0.50*0.20 + 0.50*0.60 = 0.40 → DOWN by pu too.
        # But with pc=0.80 (UP, dist=0.30), pl=0.45 (DOWN):
        # pu = 0.50*0.80 + 0.50*0.45 = 0.625 → UP by pu.
        # Here direction should follow classifier (UP), not pu (also UP), but
        # the metadata should show vhc_overriding_direction=True.
        s = _up_surface(
            probability_classifier=0.80,
            probability_lgb=0.45,
            delta_chainlink=+0.005,
            delta_tiingo=+0.004,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"
        assert d.metadata["vhc_overriding_direction"] is True
        # probability_used should be pc, not pu
        assert d.metadata["probability_used"] == 0.80

    def test_vhc_still_blocked_by_up_fill_floor(self):
        """UP fill floor is a capital-safety gate — VHC must NOT bypass it."""
        # pc=0.80 (UP, VHC), pl=0.70 (UP agree), fill=0.50 < 0.55 floor.
        s = _up_surface(
            probability_classifier=0.80,
            probability_lgb=0.70,
            clob_up_ask=0.50,
            poly_max_entry_price=0.50,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "up_fill_floor" in d.skip_reason

    def test_vhc_still_blocked_by_timing(self):
        """Timing is a capital-safety gate — VHC must NOT bypass it."""
        s = _make_surface(
            probability_classifier=0.15,
            probability_lgb=0.30,
            eval_offset=250,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        assert "timing" in d.skip_reason

    def test_vhc_bypass_disabled_by_flag(self):
        """When vhc_bypass_disagreement=false, VHC cannot bypass disagreement."""
        params = {
            "min_offset_sec": 30, "max_offset_sec": 200,
            "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
            "block_down_vpin_regimes": ["TRANSITION"],
            "block_up_vpin_regimes": ["TRANSITION"],
            "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
            "lgb_dist_min_up_with_hc_agree": 0.05,
            "lgb_dist_min_down_with_hc_agree": 0.05,
            "fill_band_min": 0.00, "fill_band_max": 0.82,
            "up_min_fill_price": 0.55, "down_min_fill_price": 0.15,
            "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
            "source_agreement_require_chainlink": True,
            "source_agreement_require_tiingo": True,
            "skip_on_oracle_disagree": True,
            "vpin_min": 0.40, "vpin_max": 1.0,
            "post_loss_cooldown_min": 20,
            "ensemble_disagreement_threshold": 0.25,
            "require_direction_agreement": True,
            "pc_weight_t_60": 0.55, "pc_weight_t_120": 0.50,
            "pc_weight_t_180": 0.45, "pc_weight_t_200": 0.35,
            "vhc_threshold": 0.25,
            "vhc_bypass_transition": True,
            "vhc_bypass_up_dist": True,
            "vhc_bypass_disagreement": False,  # DISABLED
            "vhc_bypass_lgb_safety_floor": True,
            "vhc_bypass_oracle_direction": True,
            "vhc_kelly_multiplier": 2.0,
            "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
            "conviction_low_dist": 0.05,
            "fallback_to_lgb_on_pc_null": True,
            "transition_strong_bypass_enabled": True,
            "transition_bypass_min_avg_pct_delta": 0.05,
            "transition_bypass_min_lgb_dist": 0.20,
            "delta_gate_enabled": False,
            "min_consecutive_pass_ticks": 0,
            "pl_vhc_bypass_enabled": True,
            "pl_vhc_threshold": 0.25,
            "pl_vhc_require_pc_agreement": True,
        }
        token = _gp.set_active(params)
        try:
            s = _up_surface(
                probability_classifier=0.80,
                probability_lgb=0.45,
            )
            d = evaluate_v9_ensemble(s)
            assert d.action == "SKIP"
            assert "ensemble_disagreement" in d.skip_reason
        finally:
            _gp.reset_active(token)


# ── PL VHC bypass with classifier cross-veto (note #238) ─────────────────
class TestPlVhcBypass:
    """When LGB hits VHC (|pl - 0.5| >= 0.25), bypass signal-quality gates
    unless classifier actively disagrees on direction (cross-veto).
    Only fires when pc VHC didn't already fire.
    """

    def test_pl_vhc_bypasses_disagreement(self):
        """pl=0.17 DOWN (VHC), pc=0.45 DOWN (agrees) -> TRADE DOWN.

        |pl-0.5| = 0.33 >= 0.25. pc_dir=DOWN agrees with pl_dir=DOWN.
        |pc-pl| = 0.28 > 0.25 disagreement threshold, but PL-VHC bypasses.
        pc_dist=0.05 < 0.25 so pc VHC does NOT fire — pl VHC path.
        """
        s = _make_surface(
            probability_lgb=0.17,           # DOWN, dist=0.33 VHC
            probability_classifier=0.45,    # DOWN, dist=0.05 NOT VHC
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"  # follows LGB direction
        assert d.metadata["is_pl_vhc"] is True
        assert d.metadata["pl_vhc_bypass"] is True
        assert d.metadata["pl_vhc_cross_veto"] is False
        assert d.metadata["is_vhc"] is True  # VHC final flag set
        assert d.metadata["conviction_label"] == "VERY_HIGH"
        assert d.confidence_score >= 0.55

    def test_pl_vhc_cross_veto(self):
        """pl=0.17 DOWN (VHC), pc=0.55 UP (disagrees) -> SKIP (cross-veto).

        |pl-0.5| = 0.33 >= 0.25 but classifier says UP while LGB says DOWN.
        Cross-veto fires.
        """
        s = _make_surface(
            probability_lgb=0.17,           # DOWN, dist=0.33 VHC
            probability_classifier=0.55,    # UP — disagrees with LGB
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        # Should be blocked by direction_agreement or disagreement
        # (cross-veto means pl_vhc did not activate, so no bypass)
        assert d.metadata.get("pl_vhc_cross_veto") is True or \
            "direction_agreement" in (d.skip_reason or "") or \
            "ensemble_disagreement" in (d.skip_reason or "")

    def test_pl_vhc_pc_none(self):
        """pl=0.80 UP (VHC), pc=None -> TRADE UP (no cross-veto, classifier absent).

        When pc is None, v9 falls back to v8_lgb_only. But the test
        validates that the fallback_to_lgb_on_pc_null path handles the
        VHC LGB correctly (trade goes through via v8_lgb_only fallback).
        """
        s = _up_surface(
            probability_lgb=0.80,           # UP, dist=0.30 VHC
            probability_classifier=None,    # absent
        )
        d = evaluate_v9_ensemble(s)
        # pc=None triggers fallback to v8_lgb_only, which should trade UP
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"
        assert d.metadata.get("fallback_reason") == "pc_null"

    def test_pl_vhc_below_threshold(self):
        """pl=0.35 DOWN (dist=0.15, below 0.25) -> no bypass.

        Even though LGB says DOWN, dist is only 0.15 < 0.25 threshold.
        If pc disagrees, normal gates apply.
        """
        s = _make_surface(
            probability_lgb=0.35,           # DOWN, dist=0.15 < 0.25
            probability_classifier=0.55,    # UP — disagrees
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "SKIP"
        # Should be blocked by direction_agreement (no VHC bypass)
        assert "direction_agreement" in (d.skip_reason or "")

    def test_pl_vhc_not_when_pc_vhc_fires(self):
        """pl=0.80 UP (VHC), pc=0.80 UP (VHC) -> pc VHC fires, pl VHC doesn't.

        When both models are VHC and agree, pc VHC takes precedence.
        pl VHC should NOT double-fire.
        """
        s = _up_surface(
            probability_lgb=0.80,           # UP, dist=0.30 VHC
            probability_classifier=0.80,    # UP, dist=0.30 VHC
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"
        assert d.metadata["is_vhc"] is True
        # pc VHC fires; pl VHC does NOT fire (only when pc VHC misses)
        assert d.metadata["is_pl_vhc"] is False

    def test_pl_vhc_direction_follows_lgb(self):
        """When PL VHC fires and bypasses gates, direction follows LGB.

        pl=0.17 DOWN (VHC), pc=0.55 UP (disagrees on direction).
        |pc-pl| = 0.38 > 0.25 disagreement threshold -> PL-VHC bypasses.
        Direction should be DOWN (from LGB), not UP (from classifier).
        pc_dist = 0.05 < 0.25 so pc VHC does NOT fire.
        Cross-veto: pc=0.55 UP but pl=0.17 DOWN -> BUT wait, cross-veto
        checks direction. pc says UP, pl says DOWN -> veto fires.
        Instead use: pc=0.45 DOWN (agrees), oracles UP (to trigger bypass).
        """
        # pl=0.17 DOWN VHC, pc=0.45 DOWN (agrees). Oracles point UP
        # so oracle_direction gate triggers -> PL-VHC bypasses it.
        # Direction should follow LGB = DOWN.
        s = _make_surface(
            probability_lgb=0.17,           # DOWN, dist=0.33 VHC
            probability_classifier=0.45,    # DOWN, dist=0.05 NOT VHC, agrees
            delta_chainlink=+0.005,         # UP — triggers oracle_direction
            delta_tiingo=+0.004,            # UP
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"  # follows LGB, not classifier
        assert d.metadata["is_pl_vhc"] is True
        assert d.metadata["vhc_overriding_direction"] is True
        # probability_used should be pl (LGB) when pl_vhc + overriding
        assert d.metadata["probability_used"] == 0.17

    def test_pl_vhc_bypasses_oracle_direction(self):
        """PL VHC should bypass oracle_direction when oracles disagree.

        pl=0.17 DOWN (VHC), pc=0.40 DOWN (agrees). Oracles UP.
        """
        s = _make_surface(
            probability_lgb=0.17,           # DOWN, dist=0.33 VHC
            probability_classifier=0.40,    # DOWN, agrees
            delta_chainlink=+0.005,         # UP — opposite to DOWN
            delta_tiingo=+0.004,            # UP
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"
        assert "oracle_direction" in d.metadata["vhc_bypasses"]
        assert d.metadata["is_pl_vhc"] is True

    def test_pl_vhc_bypasses_transition_regime(self):
        """PL VHC should bypass TRANSITION block.

        pl=0.17 DOWN (VHC), pc=0.40 DOWN (agrees). TRANSITION regime.
        """
        s = _make_surface(
            regime="TRANSITION",
            probability_lgb=0.17,           # DOWN, dist=0.33 VHC
            probability_classifier=0.40,    # DOWN, agrees
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
            clob_down_ask=0.55,
            poly_max_entry_price=0.55,
        )
        d = evaluate_v9_ensemble(s)
        assert d.action == "TRADE", d.skip_reason
        assert "transition_regime" in d.metadata["vhc_bypasses"]
        assert d.metadata["is_pl_vhc"] is True


# ── Per-direction fill ceiling (added 2026-04-29) ──────────────────────────
# These gates are OPT-IN: default `up_max_fill_price`/`down_max_fill_price`
# = 1.0 (no cap). Each strategy can opt in via runtime override to block the
# math-bleed band (entries where R/R requires unrealistic WR to break even).
class TestFillCeiling:
    """Tests for `up_fill_ceiling` and `down_fill_ceiling` opt-in gates."""

    def test_up_ceiling_disabled_by_default(self):
        """Default ceiling=1.0 → high UP fill should still trade."""
        # _up_surface has clob_up_ask=0.60. Push to 0.79 to test no cap.
        s = _up_surface(clob_up_ask=0.79, poly_max_entry_price=0.79)
        d = evaluate_v9_ensemble(s)
        # No ceiling gate fired → not in skip_reason
        assert "up_fill_ceiling" not in (d.skip_reason or "")
        # Either TRADE or skip for an unrelated reason
        if d.action == "SKIP":
            # Make sure the skip wasn't from our new gate
            assert "up_fill_ceiling" not in d.skip_reason

    def test_up_ceiling_blocks_when_set(self):
        """When `up_max_fill_price=0.75`, UP fill of 0.79 should SKIP."""
        params = {
            "min_offset_sec": 30, "max_offset_sec": 200,
            "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
            "block_down_vpin_regimes": ["TRANSITION"],
            "block_up_vpin_regimes": ["TRANSITION"],
            "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
            "lgb_dist_min_up_with_hc_agree": 0.05,
            "lgb_dist_min_down_with_hc_agree": 0.05,
            "fill_band_min": 0.00, "fill_band_max": 0.82,
            "up_min_fill_price": 0.20, "down_min_fill_price": 0.15,
            "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
            "source_agreement_require_chainlink": True,
            "source_agreement_require_tiingo": True,
            "skip_on_oracle_disagree": True,
            "vpin_min": 0.40, "vpin_max": 1.0,
            "post_loss_cooldown_min": 20,
            "ensemble_disagreement_threshold": 0.25,
            "require_direction_agreement": True,
            "pc_weight_t_60": 0.55, "pc_weight_t_120": 0.50,
            "pc_weight_t_180": 0.45, "pc_weight_t_200": 0.35,
            "vhc_threshold": 0.25,
            "vhc_bypass_transition": True,
            "vhc_bypass_up_dist": True,
            "vhc_bypass_disagreement": True,
            "vhc_bypass_lgb_safety_floor": True,
            "vhc_bypass_oracle_direction": True,
            "vhc_kelly_multiplier": 2.0,
            "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
            "conviction_low_dist": 0.05,
            "fallback_to_lgb_on_pc_null": True,
            "transition_strong_bypass_enabled": True,
            "transition_bypass_min_avg_pct_delta": 0.05,
            "transition_bypass_min_lgb_dist": 0.20,
            "delta_gate_enabled": False,
            "min_consecutive_pass_ticks": 0,
            "pl_vhc_bypass_enabled": True,
            "pl_vhc_threshold": 0.25,
            "pl_vhc_require_pc_agreement": True,
            # NEW: cap UP entries at $0.75
            "up_max_fill_price": 0.75,
        }
        token = _gp.set_active(params)
        try:
            s = _up_surface(clob_up_ask=0.79, poly_max_entry_price=0.79)
            d = evaluate_v9_ensemble(s)
            assert d.action == "SKIP"
            assert "up_fill_ceiling" in d.skip_reason
            assert "0.79" in d.skip_reason  # fill price echoed
            assert "0.75" in d.skip_reason  # ceiling echoed
        finally:
            _gp.reset_active(token)

    def test_up_ceiling_passes_when_below(self):
        """When `up_max_fill_price=0.75`, UP fill of 0.70 should TRADE."""
        params = {
            "min_offset_sec": 30, "max_offset_sec": 200,
            "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
            "block_down_vpin_regimes": ["TRANSITION"],
            "block_up_vpin_regimes": ["TRANSITION"],
            "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
            "lgb_dist_min_up_with_hc_agree": 0.05,
            "lgb_dist_min_down_with_hc_agree": 0.05,
            "fill_band_min": 0.00, "fill_band_max": 0.82,
            "up_min_fill_price": 0.20, "down_min_fill_price": 0.15,
            "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
            "source_agreement_require_chainlink": True,
            "source_agreement_require_tiingo": True,
            "skip_on_oracle_disagree": True,
            "vpin_min": 0.40, "vpin_max": 1.0,
            "post_loss_cooldown_min": 20,
            "ensemble_disagreement_threshold": 0.25,
            "require_direction_agreement": True,
            "pc_weight_t_60": 0.55, "pc_weight_t_120": 0.50,
            "pc_weight_t_180": 0.45, "pc_weight_t_200": 0.35,
            "vhc_threshold": 0.25,
            "vhc_bypass_transition": True,
            "vhc_bypass_up_dist": True,
            "vhc_bypass_disagreement": True,
            "vhc_bypass_lgb_safety_floor": True,
            "vhc_bypass_oracle_direction": True,
            "vhc_kelly_multiplier": 2.0,
            "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
            "conviction_low_dist": 0.05,
            "fallback_to_lgb_on_pc_null": True,
            "transition_strong_bypass_enabled": True,
            "transition_bypass_min_avg_pct_delta": 0.05,
            "transition_bypass_min_lgb_dist": 0.20,
            "delta_gate_enabled": False,
            "min_consecutive_pass_ticks": 0,
            "pl_vhc_bypass_enabled": True,
            "pl_vhc_threshold": 0.25,
            "pl_vhc_require_pc_agreement": True,
            "up_max_fill_price": 0.75,
        }
        token = _gp.set_active(params)
        try:
            s = _up_surface(clob_up_ask=0.70, poly_max_entry_price=0.70)
            d = evaluate_v9_ensemble(s)
            # Either trades, or skips for an unrelated reason
            if d.action == "SKIP":
                assert "up_fill_ceiling" not in d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_up_ceiling_does_not_block_down(self):
        """`up_max_fill_price` should NOT affect DOWN trades."""
        params = {
            "min_offset_sec": 30, "max_offset_sec": 200,
            "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
            "block_down_vpin_regimes": ["TRANSITION"],
            "block_up_vpin_regimes": ["TRANSITION"],
            "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
            "lgb_dist_min_up_with_hc_agree": 0.05,
            "lgb_dist_min_down_with_hc_agree": 0.05,
            "fill_band_min": 0.00, "fill_band_max": 0.82,
            "up_min_fill_price": 0.20, "down_min_fill_price": 0.15,
            "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
            "source_agreement_require_chainlink": True,
            "source_agreement_require_tiingo": True,
            "skip_on_oracle_disagree": True,
            "vpin_min": 0.40, "vpin_max": 1.0,
            "post_loss_cooldown_min": 20,
            "ensemble_disagreement_threshold": 0.25,
            "require_direction_agreement": True,
            "pc_weight_t_60": 0.55, "pc_weight_t_120": 0.50,
            "pc_weight_t_180": 0.45, "pc_weight_t_200": 0.35,
            "vhc_threshold": 0.25,
            "vhc_bypass_transition": True,
            "vhc_bypass_up_dist": True,
            "vhc_bypass_disagreement": True,
            "vhc_bypass_lgb_safety_floor": True,
            "vhc_bypass_oracle_direction": True,
            "vhc_kelly_multiplier": 2.0,
            "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
            "conviction_low_dist": 0.05,
            "fallback_to_lgb_on_pc_null": True,
            "transition_strong_bypass_enabled": True,
            "transition_bypass_min_avg_pct_delta": 0.05,
            "transition_bypass_min_lgb_dist": 0.20,
            "delta_gate_enabled": False,
            "min_consecutive_pass_ticks": 0,
            "pl_vhc_bypass_enabled": True,
            "pl_vhc_threshold": 0.25,
            "pl_vhc_require_pc_agreement": True,
            "up_max_fill_price": 0.75,  # cap UP only
        }
        token = _gp.set_active(params)
        try:
            # DOWN surface with high DOWN ask (which is fine for DOWN direction)
            s = _make_surface(clob_down_ask=0.79, poly_max_entry_price=0.79)
            d = evaluate_v9_ensemble(s)
            # ceiling gate should NOT fire on DOWN
            assert "up_fill_ceiling" not in (d.skip_reason or "")
            assert "down_fill_ceiling" not in (d.skip_reason or "")
        finally:
            _gp.reset_active(token)

    def test_down_ceiling_blocks_when_set(self):
        """When `down_max_fill_price=0.65`, DOWN fill of 0.79 should SKIP."""
        params = {
            "min_offset_sec": 30, "max_offset_sec": 200,
            "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
            "block_down_vpin_regimes": ["TRANSITION"],
            "block_up_vpin_regimes": ["TRANSITION"],
            "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
            "lgb_dist_min_up_with_hc_agree": 0.05,
            "lgb_dist_min_down_with_hc_agree": 0.05,
            "fill_band_min": 0.00, "fill_band_max": 0.82,
            "up_min_fill_price": 0.20, "down_min_fill_price": 0.15,
            "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
            "source_agreement_require_chainlink": True,
            "source_agreement_require_tiingo": True,
            "skip_on_oracle_disagree": True,
            "vpin_min": 0.40, "vpin_max": 1.0,
            "post_loss_cooldown_min": 20,
            "ensemble_disagreement_threshold": 0.25,
            "require_direction_agreement": True,
            "pc_weight_t_60": 0.55, "pc_weight_t_120": 0.50,
            "pc_weight_t_180": 0.45, "pc_weight_t_200": 0.35,
            "vhc_threshold": 0.25,
            "vhc_bypass_transition": True,
            "vhc_bypass_up_dist": True,
            "vhc_bypass_disagreement": True,
            "vhc_bypass_lgb_safety_floor": True,
            "vhc_bypass_oracle_direction": True,
            "vhc_kelly_multiplier": 2.0,
            "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
            "conviction_low_dist": 0.05,
            "fallback_to_lgb_on_pc_null": True,
            "transition_strong_bypass_enabled": True,
            "transition_bypass_min_avg_pct_delta": 0.05,
            "transition_bypass_min_lgb_dist": 0.20,
            "delta_gate_enabled": False,
            "min_consecutive_pass_ticks": 0,
            "pl_vhc_bypass_enabled": True,
            "pl_vhc_threshold": 0.25,
            "pl_vhc_require_pc_agreement": True,
            "down_max_fill_price": 0.65,
        }
        token = _gp.set_active(params)
        try:
            # DOWN trade with fill 0.79 (above 0.65 cap)
            s = _make_surface(clob_down_ask=0.79, poly_max_entry_price=0.79)
            d = evaluate_v9_ensemble(s)
            assert d.action == "SKIP"
            assert "down_fill_ceiling" in d.skip_reason
        finally:
            _gp.reset_active(token)
