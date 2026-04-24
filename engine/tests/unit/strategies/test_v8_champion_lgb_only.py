"""Tests for v8_champion_lgb_only — LGB-only fine-tune variant.

Covers the 4 gates new/changed vs v8_champion:
  - lgb_bucket (replaces ensemble_bucket; no classifier dependency)
  - vpin_regime_direction (blocks DOWN in TRANSITION)
  - fill-band relaxed to [0.00, 0.82] + down_fill_floor 0.15
  - post_loss_cooldown (20 min default)

Test fixtures mirror test_v8_champion.py's ``_make_surface`` / ``_up_surface``
pattern so any future surface-field additions only need updating in one place
once the shared helper lands (out of scope for this PR).
"""
from __future__ import annotations

import datetime as _dt
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only,
    record_loss,
    reset_cooldown,
    reset_all_confirmations,
)
from strategies import gate_params as _gp
from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)


def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface landing inside every gate.

    Mirrors test_v8_champion._make_surface but populated for the LGB-only
    path (LGB p_up=0.30 → DOWN, dist=0.20 ≥ 0.10 floor). Hour=12 (allowed),
    vpin=0.55 (inside [0.40,1.0]), v4_regime=volatile_trend (tradeable),
    vpin regime=NORMAL (not TRANSITION), clob_down_ask=0.55 (inside
    [0.00,0.82] and ≥ 0.15 DOWN floor), oracles aligned DOWN.
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


def _up_surface(**overrides) -> FullDataSurface:
    """Symmetric UP-direction surface. LGB p_up=0.70 → UP, dist=0.20 ≥ 0.15."""
    defaults = dict(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.70, probability_classifier=None,
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
    """Bind the YAML defaults for v8_champion_lgb_only around each test.

    Standalone hook calls skip the registry's ``_evaluate_one`` wrapper,
    which is where gate_params normally gets set from the config.
    Without this the hook reads every param via env/default; that's fine
    for most, but the new down_fill_floor / block_down_vpin_regimes /
    post_loss_cooldown defaults come from YAML. Bind them explicitly so
    tests exercise the values an operator ships.
    """
    params = {
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": [
            "volatile_trend", "chop", "risk_off", "calm_trend",
        ],
        "block_down_vpin_regimes": ["TRANSITION"],
        "block_up_vpin_regimes": [],
        "use_classifier_bucket": False,
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.55,
        "up_require_both_buckets": False,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        "transition_strong_bypass_enabled": True,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        # Disable new features for legacy tests — tested separately
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
    }
    token = _gp.set_active(params)
    reset_cooldown()
    reset_all_confirmations()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations()


# ── Registry load sanity ───────────────────────────────────────────────────
@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def test_registered_as_live(registry):
    assert "v8_champion_lgb_only" in registry.strategy_names
    cfg = registry.configs["v8_champion_lgb_only"]
    assert cfg.mode == "LIVE"
    assert cfg.version == "8.0.0-lgb-0.2"
    assert cfg.timescale == "5m"
    assert cfg.asset == "BTC"
    gp = cfg.gate_params
    for key in (
        "min_offset_sec",
        "max_offset_sec",
        "tradeable_v4_regimes",
        "block_down_vpin_regimes",
        "lgb_dist_min_down",
        "lgb_dist_min_up",
        "fill_band_min",
        "fill_band_max",
        "down_min_fill_price",
        "post_loss_cooldown_min",
    ):
        assert key in gp, f"missing gate_param: {key}"
    assert gp["fill_band_min"] == 0.00
    assert gp["fill_band_max"] == 0.82
    assert gp["down_min_fill_price"] == 0.15
    assert gp["post_loss_cooldown_min"] == 20
    assert gp["block_down_vpin_regimes"] == ["TRANSITION"]


# ── Default accept path ────────────────────────────────────────────────────
def test_default_down_surface_trades():
    d = evaluate_v8_champion_lgb_only(_make_surface())
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "DOWN"


def test_default_up_surface_trades():
    d = evaluate_v8_champion_lgb_only(_up_surface())
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "UP"


# ── LGB bucket ─────────────────────────────────────────────────────────────
class TestLgbBucket:
    def test_up_low_dist_skip(self):
        # p_up=0.55, dist=0.05 < 0.15 UP floor
        s = _up_surface(probability_lgb=0.55)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "lgb_bucket" in d.skip_reason

    def test_up_high_dist_trade(self):
        s = _up_surface(probability_lgb=0.80, clob_up_ask=0.60)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "UP"

    def test_down_dist_0_12_passes(self):
        # DOWN min_dist = 0.10; p_up=0.38 gives dist=0.12
        s = _make_surface(probability_lgb=0.38, v4_regime="chop")
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"

    def test_down_low_dist_skip(self):
        # p_up=0.45, dist=0.05 < 0.10 DOWN floor
        s = _make_surface(probability_lgb=0.45)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "lgb_bucket" in d.skip_reason

    def test_null_classifier_still_fires(self):
        # Confirms LGB-only works when classifier is None
        s = _up_surface(probability_lgb=0.70, probability_classifier=None)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason

    def test_probability_lgb_null_skips(self):
        s = _make_surface(probability_lgb=None)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "lgb_bucket" in d.skip_reason


# ── Regime direction (vpin regime) ─────────────────────────────────────────
class TestRegimeDirection:
    def test_down_in_transition_blocked(self):
        s = _make_surface(probability_lgb=0.38, regime="TRANSITION")
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_up_in_transition_allowed(self):
        s = _up_surface(
            probability_lgb=0.70, regime="TRANSITION", clob_up_ask=0.60
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason

    def test_down_in_normal_allowed(self):
        s = _make_surface(probability_lgb=0.38, regime="NORMAL")
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason


# ── Fill band ──────────────────────────────────────────────────────────────
class TestFillBand:
    def test_down_at_0_15_passes(self):
        # Tight DOWN floor; tiingo delta must still agree down
        s = _make_surface(
            probability_lgb=0.38, clob_down_ask=0.15, poly_max_entry_price=0.15
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason

    def test_down_below_0_15_blocked(self):
        s = _make_surface(
            probability_lgb=0.38, clob_down_ask=0.10, poly_max_entry_price=0.10
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "down_fill_floor" in d.skip_reason

    def test_high_above_0_82_blocked(self):
        s = _up_surface(
            probability_lgb=0.80, clob_up_ask=0.90, poly_max_entry_price=0.90
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "fill_band" in d.skip_reason

    def test_up_below_up_floor_blocked(self):
        # UP fill_band passes (0.45 in [0.00, 0.82]) but UP floor (0.55) rejects
        s = _up_surface(
            probability_lgb=0.80, clob_up_ask=0.45, poly_max_entry_price=0.45
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "up_fill_floor" in d.skip_reason

    def test_up_at_low_passes_band_blocked_by_floor(self):
        # Dropping fill_band LOW to 0.00 means UP at 0.10 passes fill_band
        # but UP floor (0.55) still rejects.
        s = _up_surface(
            probability_lgb=0.80, clob_up_ask=0.10, poly_max_entry_price=0.10
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "up_fill_floor" in d.skip_reason


# ── Cooldown ───────────────────────────────────────────────────────────────
class TestCooldown:
    def test_no_cooldown_when_no_prior_loss(self):
        reset_cooldown()
        s = _up_surface(probability_lgb=0.70, clob_up_ask=0.60)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason

    def test_cooldown_blocks_within_window(self):
        record_loss(int(time.time()) - 600)  # loss 10 min ago, cooldown 20 min
        s = _up_surface(probability_lgb=0.70, clob_up_ask=0.60)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "post_loss_cooldown" in d.skip_reason

    def test_cooldown_expires_after_20min(self):
        record_loss(int(time.time()) - 1300)  # loss 21.6 min ago
        s = _up_surface(probability_lgb=0.70, clob_up_ask=0.60)
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason


# ── Oracle direction ───────────────────────────────────────────────────────
def test_oracle_disagree_skips():
    # LGB says UP but oracles point DOWN → oracle_direction gate fires
    s = _up_surface(
        probability_lgb=0.70,
        clob_up_ask=0.60,
        delta_chainlink=-0.005,
        delta_tiingo=-0.004,
    )
    d = evaluate_v8_champion_lgb_only(s)
    assert d.action == "SKIP"
    assert "oracle_direction" in d.skip_reason


# ── Timing ─────────────────────────────────────────────────────────────────
def test_timing_out_of_window_skips():
    s = _make_surface(eval_offset=250)
    d = evaluate_v8_champion_lgb_only(s)
    assert d.action == "SKIP"
    assert "timing" in d.skip_reason


# ── TRANSITION strong-oracle bypass (note #228, 2026-04-24) ────────────────
class TestTransitionBypass:
    """Bypass TRANSITION regime block when chainlink+tiingo agree and LGB strong.

    Uses direct _should_bypass_transition where possible to isolate the rule
    from the rest of the gate stack, and at least one end-to-end test that
    exercises the v8 hook integration.
    """

    def test_down_transition_bypass_with_strong_oracles(self):
        # TRANSITION + DOWN + oracles strongly DOWN + LGB dist 0.30 (strong).
        # Default _block_down_vpin_regimes=[TRANSITION] — without the bypass
        # this would SKIP.
        s = _make_surface(
            regime="TRANSITION",
            probability_lgb=0.20,            # dist = 0.30 >= 0.20 lgb floor
            delta_chainlink=-0.0007,         # -0.07% fraction
            delta_tiingo=-0.0008,            # -0.08% fraction
            delta_binance=-0.0001,           # excluded from bypass
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "TRADE", d.skip_reason
        assert d.direction == "DOWN"
        gate_names = [g["gate"] for g in d.metadata["gate_results"]]
        assert "vpin_regime_direction" in gate_names
        regime_gate = [
            g for g in d.metadata["gate_results"]
            if g["gate"] == "vpin_regime_direction"
        ][-1]
        assert regime_gate["passed"] is True
        assert "transition_strong_bypass" in regime_gate.get("reason", "")

    def test_down_transition_NO_bypass_weak_oracles(self):
        # Same TRANSITION + DOWN but avg oracle magnitude below threshold.
        s = _make_surface(
            regime="TRANSITION",
            probability_lgb=0.20,
            delta_chainlink=-0.0002,         # avg = 0.025% < 0.05%
            delta_tiingo=-0.0003,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_up_transition_bypass_with_strong_oracles(self, monkeypatch):
        # Symmetric UP case — need block_up_vpin_regimes active for block
        # to fire at all (default v8 YAML is empty for UP).
        params = dict(_gp._ACTIVE.get() or {})  # type: ignore[attr-defined]
        params["block_up_vpin_regimes"] = ["TRANSITION"]
        token = _gp.set_active(params)
        try:
            s = _up_surface(
                regime="TRANSITION",
                probability_lgb=0.80,         # dist = 0.30
                delta_chainlink=0.0007,
                delta_tiingo=0.0008,
                clob_up_ask=0.65,
                poly_max_entry_price=0.65,
            )
            d = evaluate_v8_champion_lgb_only(s)
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "UP"
        finally:
            _gp.reset_active(token)

    def test_bypass_blocked_when_lgb_weak(self):
        # Oracles strong but LGB dist 0.12 < 0.20 threshold → no bypass.
        # NB: LGB dist floor for DOWN is 0.10, so the lgb_bucket gate passes
        # with p_up=0.38 (dist=0.12); bypass's 0.20 threshold is the decider.
        s = _make_surface(
            regime="TRANSITION",
            probability_lgb=0.38,            # dist = 0.12 passes lgb_bucket
            delta_chainlink=-0.0007,
            delta_tiingo=-0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"
        assert "vpin_regime_direction" in d.skip_reason

    def test_bypass_blocked_when_oracles_disagree(self):
        # chainlink DOWN but tiingo UP — oracle_direction fires first anyway,
        # but the regime-gate bypass check would also reject.
        s = _make_surface(
            regime="TRANSITION",
            probability_lgb=0.20,            # bet DOWN
            delta_chainlink=-0.0007,
            delta_tiingo=+0.0008,
            clob_down_ask=0.65,
            poly_max_entry_price=0.65,
        )
        d = evaluate_v8_champion_lgb_only(s)
        assert d.action == "SKIP"

    def test_bypass_respects_disabled_flag(self, monkeypatch):
        params = dict(_gp._ACTIVE.get() or {})  # type: ignore[attr-defined]
        params["transition_strong_bypass_enabled"] = False
        token = _gp.set_active(params)
        try:
            s = _make_surface(
                regime="TRANSITION",
                probability_lgb=0.20,
                delta_chainlink=-0.0007,
                delta_tiingo=-0.0008,
                clob_down_ask=0.65,
                poly_max_entry_price=0.65,
            )
            d = evaluate_v8_champion_lgb_only(s)
            assert d.action == "SKIP"
            assert "vpin_regime_direction" in d.skip_reason
        finally:
            _gp.reset_active(token)


# ── Hour block ─────────────────────────────────────────────────────────────
def test_h03_blocked():
    ts_h3 = int(
        _dt.datetime(2024, 4, 13, 3, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    )
    s = _make_surface(window_ts=ts_h3)
    d = evaluate_v8_champion_lgb_only(s)
    assert d.action == "SKIP"
    assert "utc_hour_block" in d.skip_reason
