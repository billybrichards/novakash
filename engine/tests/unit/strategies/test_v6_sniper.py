"""Tests for v6_sniper — bidirectional ensemble sniper.

Covers the 14 spec test cases:
  1-4  agree_strong bucket (accept + reject paths)
  5-8  pegged_path1 bucket (LGB-indifferent / agree / opposite / DOWN extreme)
  9    mid_conf block
  10   no_eval block (path1=None)
  11   vpin_min floor
  12   blocked_utc_hours
  13   source_agreement (chainlink/tiingo disagree)
  14   prefer_raw_probability → metadata records raw source

Strategy hooks live in ``engine/strategies/configs/v6_sniper.py`` and are
loaded via importlib by ``StrategyRegistry``. We exercise them through
the registry the same way ``test_v5_ensemble.py`` does so the real load
path is under test, not the function in isolation.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)


# ── Fixtures ────────────────────────────────────────────────────────────────
def _make_surface(**overrides) -> FullDataSurface:
    """Default surface that lands inside v6_sniper's accept window.

    v6.1.0: Direction=DOWN, both models agree, |dist|=0.25 → agree_strong bucket
    (strong threshold tightened from 0.20 to 0.22 in v6.1.0, with float-precision
    headroom left by using 0.25 here). UTC hour=12 (not blocked). VPIN=0.55
    (above floor). Both sources present and agree.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,  # 12:00 UTC
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.25, v2_probability_raw=0.25,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        # Path 1 ensemble — both DOWN, |p_up - 0.5| = 0.25 (safely above 0.22)
        probability_lgb=0.30, probability_classifier=0.25,
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.25,
        poly_confidence_distance=0.25, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        # v6.1.0: put CLOB asks OUTSIDE the [0.35, 0.60] mid-range fill
        # gate so the base surface lands in the high-fill band and
        # pre-v6.1.0 tests continue to exercise their original paths.
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.63,
        clob_down_ask=0.65, clob_implied_up=0.64,
        gamma_up_price=0.45, gamma_down_price=0.55,
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
    # v6.1.2 introduced Tier-1 gates (require_lgb_aligned, max_head_disagreement,
    # min_confidence_score) that — by design — reject many of the marginal
    # agree_strong / pegged_path1 fixtures this file was written against.
    # These tests cover the v6.1.0/v6.1.1 bucket + freshness logic below the
    # new gates, so we disable the v6.1.2 gates at fixture scope to keep the
    # original regression coverage intact. v6.1.2 behaviour itself is pinned
    # by ``test_v6_sniper_v6_1_2.py``.
    cfg = reg.configs["v6_sniper"]
    cfg.gate_params["require_lgb_aligned"] = False
    cfg.gate_params["max_head_disagreement"] = 0.0
    cfg.gate_params["min_confidence_score"] = 0.0
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v6_sniper", registry.configs["v6_sniper"], surface
    )


# ── Registry load sanity ────────────────────────────────────────────────────
def test_v6_sniper_registered_as_live(registry):
    assert "v6_sniper" in registry.strategy_names
    cfg = registry.configs["v6_sniper"]
    assert cfg.mode == "LIVE"
    # Version pin relaxed in v6.1.2 — this file covers code paths shared
    # across 6.1.x and forward; see ``test_v6_sniper_v6_1_2.test_version_bumped``
    # for the exact-version pin on the currently-LIVE release.
    assert cfg.version.startswith("6.1.")
    assert cfg.timescale == "5m"


# ── #1-#4 agree_strong bucket ──────────────────────────────────────────────
@pytest.mark.skip(
    reason=(
        "v6.1.3 EMERGENCY DOWN-only: UP-direction TRADE paths are blocked "
        "by impossible up_bucket_* thresholds (1.01). Test will re-pass once "
        "UP is re-enabled by reverting to v6.1.2 values (0.28 / 0.95)."
    )
)
def test_1_agree_strong_both_up_accepts(registry):
    """Both models agree UP + stringent UP thresholds -> ACCEPT.

    v6.1.1: UP requires BOTH agree_strong (|dist| >= 0.28) AND pegged_path1
    (path1 >= 0.95). Updated from the v6.1.0 surface (dist=0.25) to
    clear the tightened UP gate: dist=0.46, path1=0.96.
    """
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.96,
        poly_confidence_distance=0.46,
        probability_lgb=0.90, probability_classifier=0.96,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert decision.direction == "UP"
    # pegged_path1 is checked first and matches first (path1 >= 0.95), so this
    # surface classifies as pegged_path1 rather than agree_strong. Both accept
    # paths are covered by tests 2 (DOWN agree_strong) and 5/6 (pegged_path1).
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_2_agree_strong_both_down_accepts(registry):
    """v6.1.0: Both models agree DOWN + dist=0.25 -> ACCEPT."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.30, probability_classifier=0.26,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "agree_strong"


def test_3_agree_strong_models_disagree_rejects(registry):
    """Models disagree + dist=0.25 -> REJECT (fall through to mid_conf block).

    probability_up=0.25 (DOWN), LGB=0.70 (UP). LGB opposes trade direction
    AND is not pegged, so bucket is neither agree_strong nor pegged_path1
    → mid_conf_blocked.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.70, probability_classifier=0.25,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")


def test_4_agree_strong_dist_too_low_rejects(registry):
    """Both agree UP but dist=0.15 -> below strong threshold → mid_conf block."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.65,
        poly_confidence_distance=0.15,
        probability_lgb=0.63, probability_classifier=0.68,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")


# ── #5-#8 pegged_path1 bucket ──────────────────────────────────────────────
@pytest.mark.skip(
    reason=(
        "v6.1.3 EMERGENCY DOWN-only: UP-direction TRADE paths blocked. "
        "See DOWN-side pegged_path1 coverage in test_8 below; UP coverage "
        "returns once emergency UP thresholds are reverted."
    )
)
def test_5_pegged_path1_lgb_indifferent_accepts(registry):
    """path1=0.97 + LGB=0.52 (indifferent) -> ACCEPT (relaxed).

    |p_lgb - 0.5| = 0.02 < 0.10 opposite-block threshold, and even if LGB
    were technically "DOWN" it's not strong enough to block a pegged UP
    path1.
    """
    # probability_up = 0.97 → dist = 0.47 — also satisfies agree_strong if
    # models agreed, but LGB=0.52 (indifferent) means we fall into the
    # pegged_path1 branch which is checked FIRST by _classify_bucket.
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.52, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


@pytest.mark.skip(
    reason=(
        "v6.1.3 EMERGENCY DOWN-only: UP-direction TRADE paths blocked. "
        "UP coverage returns once emergency UP thresholds are reverted."
    )
)
def test_6_pegged_path1_lgb_agrees_accepts(registry):
    """path1=0.97 + LGB=0.65 (agrees) -> ACCEPT."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.65, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


@pytest.mark.skip(
    reason=(
        "v6.1.3 EMERGENCY DOWN-only: UP-direction bucket gate short-circuits "
        "before pegged_path1_blocked_by_lgb is reachable (bucket falls to "
        "mid_conf_blocked). DOWN-side coverage of lgb-blocks-pegged remains "
        "implicit via test_classify_bucket logic; full path returns once UP "
        "emergency thresholds are reverted."
    )
)
def test_7_pegged_path1_lgb_strongly_opposite_rejects(registry):
    """path1=0.97 (UP) + LGB=0.30 (DOWN, |dist|=0.20 > 0.10) -> REJECT."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.30, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "pegged_path1_blocked_by_lgb" in (decision.skip_reason or "")


def test_8_pegged_path1_down_lgb_indifferent_accepts(registry):
    """path1=0.03 + LGB=0.48 (indifferent, DOWN side) -> ACCEPT (relaxed)."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.03,
        probability_lgb=0.48, probability_classifier=0.03,
        delta_chainlink=-0.01, delta_tiingo=-0.009, delta_binance=-0.01,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


# ── #9 mid_conf block ──────────────────────────────────────────────────────
def test_9_mid_conf_blocked(registry):
    """path1=0.65, lgb=0.62, dist=0.14 -> REJECT (neither strong nor pegged)."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.64,
        poly_confidence_distance=0.14,
        probability_lgb=0.62, probability_classifier=0.65,
        delta_chainlink=+0.002, delta_tiingo=+0.002, delta_binance=+0.002,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")
    assert decision.metadata["conviction_bucket"] == "mid_conf_blocked"


# ── #10 no_eval block ──────────────────────────────────────────────────────
def test_10_no_eval_blocked_when_path1_none(registry):
    """path1=None -> REJECT on path1 freshness gate."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.28,
        probability_lgb=0.30, probability_classifier=None,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "no_eval_blocked" in (decision.skip_reason or "")
    assert decision.metadata["conviction_bucket"] == "no_eval_blocked"


# ── #11 vpin_min ───────────────────────────────────────────────────────────
def test_11_vpin_below_floor_rejects(registry):
    """vpin=0.40 -> REJECT (below 0.45 floor)."""
    surface = _make_surface(vpin=0.40)
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "vpin_too_low" in (decision.skip_reason or "")


# ── #12 blocked_utc_hours (v6.0.1: disabled by default) ────────────────────
def test_12_blocked_utc_hours_disabled_by_default(registry):
    """v6.0.1 sets blocked_utc_hours=[] in YAML (removed per Billy). A
    window at 07:30 UTC should NOT be skipped for time-of-day reasons
    anymore. Regression guard so a later PR that sets the YAML back to
    [7,8,9] shows up as a test diff rather than silent reactivation.
    """
    window_ts = int(
        _dt.datetime(2026, 4, 19, 7, 30, tzinfo=_dt.timezone.utc).timestamp()
    )
    surface = _make_surface(window_ts=window_ts, hour_utc=7)
    decision = _evaluate(registry, surface)
    # Default happy-path surface → should TRADE with no time-of-day skip.
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # Sanity: skip_reason shouldn't contain "blocked_utc" for any reason.
    assert "blocked_utc" not in (decision.skip_reason or "")


# ── #13 source_agreement (v6.0.3: disagreement non-blocking by default) ────
def test_13_source_disagreement_does_not_reject(registry):
    """v6.0.3 sets ``skip_on_oracle_disagree: false`` in YAML — chainlink
    or tiingo direction disagreement no longer blocks. Base surface has
    poly_direction=DOWN; inverting chainlink+tiingo to UP must now
    still result in TRADE (v6's conviction + VPIN + freshness gates
    carry the selectivity load).
    """
    surface = _make_surface(
        delta_chainlink=+0.005, delta_tiingo=+0.004,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} "
        f"skip_reason={decision.skip_reason}"
    )
    # Oracle disagreement is STILL surfaced in gates metadata — just non-fatal.
    assert decision.metadata.get("gate_results") is not None


# ── #14 prefer_raw_probability ─────────────────────────────────────────────
def test_14_prefer_raw_records_raw_source(registry):
    """With prefer_raw_probability=true (YAML default), the hook reads raw
    poly_confidence and records read_probability_source='raw' in
    metadata.

    This test only verifies the raw path because the calibrated field
    (``surface.probability_up_calibrated``) is not yet on FullDataSurface
    — it arrives with engine-side PR #281. Until then, prefer_raw=false
    would fall back to raw anyway, so the test of mode A (raw) is the
    load-bearing case.

    v6.1.1: switched the surface to DOWN so the stricter UP gate does
    not gate the prefer_raw metadata check. The probability-source
    recording logic is direction-agnostic.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.28, probability_classifier=0.22,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.metadata["read_probability_source"] == "raw"
    assert decision.metadata["probability_raw"] == pytest.approx(0.25)
    # calibrated field absent today → logged as None.
    assert decision.metadata["probability_calibrated"] is None
    # Bucket carries the winning decision.
    assert decision.metadata["conviction_bucket"] == "agree_strong"


# ── Mode-flip regression (live lineup: v6_sniper sole LIVE) ────────────────
def test_v5_ensemble_is_ghost(registry):
    # 2026-04-20: v5_ensemble flipped LIVE → GHOST per Billy. Today's data:
    # v6_sniper 9W/1L (90%, +$28.62) vs v5_ensemble 4W/3L (57%, +$0.59).
    # v6_sniper is sole LIVE; v5_ensemble shadows.
    assert registry.configs["v5_ensemble"].mode == "GHOST"


def test_v5_fresh_stays_ghost(registry):
    assert registry.configs["v5_fresh"].mode == "GHOST"


def test_v4_fusion_ghost_reference(registry):
    # v4_fusion kept as GHOST reference baseline (same signal source as
    # v5_ensemble; audit-winner history in comments).
    assert registry.configs["v4_fusion"].mode == "GHOST"


def test_v4_down_only_stays_ghost(registry):
    """Sanity: v4_down_only is now LIVE (went LIVE with v2.2.0 today per
    handover). Earlier v6 spec assumed it stayed GHOST; test kept as a
    regression guard that the mode is at least explicitly set.
    """
    assert registry.configs["v4_down_only"].mode in ("LIVE", "GHOST")


# ── v6.0.1 additions: risk_off regime pass + pegged boundary tests ─────────
def test_15_risk_off_regime_rejected(registry):
    """v6.0.1 added risk_off to tradeable_v4_regimes; PR #313 (volatile-only
    Montreal parity) restricted the allowlist to ``[volatile_trend]``.
    v6.1.2 inherits that restriction — a risk_off window must SKIP with
    ``regime_not_tradeable``, not TRADE.
    """
    surface = _make_surface(v4_regime="risk_off")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "regime_not_tradeable" in (decision.skip_reason or ""), decision.skip_reason


def test_15b_chop_regime_still_rejected(registry):
    """chop stays blocked per Billy (only calm_trend / volatile_trend /
    risk_off in the allowlist)."""
    surface = _make_surface(v4_regime="chop")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "regime_not_tradeable" in (decision.skip_reason or "")


@pytest.mark.skip(
    reason=(
        "v6.1.3 EMERGENCY DOWN-only: UP peg boundary test asserts TRADE; "
        "UP now blocked by impossible thresholds. Reverts once UP re-enabled."
    )
)
def test_16_pegged_high_boundary_just_inside(registry):
    """v6.1.1: UP peg threshold is 0.95, so path1=0.96 just inside ACCEPTs.

    Also requires dist >= 0.28 AND pegged (up_require_both_buckets). With
    path1=0.96 and LGB=0.90, both buckets are satisfied.
    """
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.96,
        poly_confidence_distance=0.46,
        probability_lgb=0.90, probability_classifier=0.96,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_17_pegged_high_boundary_just_outside(registry):
    """v6.1.0: path1=0.91 + LGB=0.50 (indifferent) -> neither pegged (<0.92) nor
    agree_strong, falls into mid_conf block."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.50, probability_classifier=0.91,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    # 0.91 < 0.92 pegged threshold; LGB=0.50 means models don't agree on
    # direction with any force → agree_strong fails too. mid_conf fallback.
    assert decision.action == "SKIP"


def test_18_pegged_low_boundary_just_inside(registry):
    """v6.1.0: path1=0.07 + LGB=0.50 (indifferent) -> ACCEPT (just inside 0.08 threshold)."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.07,
        probability_lgb=0.50, probability_classifier=0.07,
        delta_chainlink=-0.01, delta_tiingo=-0.009, delta_binance=-0.01,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_19_pegged_low_boundary_just_outside(registry):
    """v6.1.0: path1=0.09 + LGB=0.45 + blended dist=0.05 -> neither pegged
    (path1 > 0.08) nor agree_strong (dist < 0.22) -> mid_conf block.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.45,
        poly_confidence_distance=0.05,
        probability_lgb=0.45, probability_classifier=0.09,
        delta_chainlink=-0.005, delta_tiingo=-0.004, delta_binance=-0.005,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"


def test_21_risk_off_override_fires_when_oracles_align(registry):
    """v6.0.4: sister-repo vetoes with reason=regime_risk_off but
    chainlink+tiingo both agree with trade direction AND dist ≥ 0.20 →
    override fires, TRADE allowed.
    """
    surface = _make_surface(
        poly_trade_advised=False,
        poly_reason="regime_risk_off",
        poly_direction="DOWN",
        poly_confidence=0.07,
        poly_confidence_distance=0.43,
        probability_lgb=0.12, probability_classifier=0.08,
        delta_chainlink=-0.0005, delta_tiingo=-0.0009,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE via risk_off override, got {decision.action} "
        f"skip_reason={decision.skip_reason}"
    )
    gate_names = [g.get("gate") for g in decision.metadata.get("gate_results", [])]
    assert "v6_risk_off_override" in gate_names


def test_22_risk_off_override_blocks_when_oracles_opposite(registry):
    """v6.0.4: same veto but chainlink UP vs trade DOWN → override fails,
    v6 skips with trade_not_advised.
    """
    surface = _make_surface(
        poly_trade_advised=False,
        poly_reason="regime_risk_off",
        poly_direction="DOWN",
        poly_confidence=0.07,
        poly_confidence_distance=0.43,
        probability_lgb=0.12, probability_classifier=0.08,
        delta_chainlink=+0.0005, delta_tiingo=+0.0009,   # OPPOSITE
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "trade_not_advised" in (decision.skip_reason or "")


def test_20_entry_cap_override_applied(registry):
    """v6.0.1: entry_cap_override=0.85 in YAML overrides surface.poly_max_entry_price."""
    surface = _make_surface(poly_max_entry_price=0.65)  # surface default < override
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.entry_cap == pytest.approx(0.85), (
        f"expected 0.85, got {decision.entry_cap}"
    )


# ── Per-inference path1 timestamp tests ────────────────────────────────────
# These tests pin the fix that replaces the surface-assembled-at proxy with
# a per-classifier-inference timestamp (probability_classifier_inferred_at).
# YAML default ``path1_max_age_s`` is 90s. The tests avoid hard-coding that
# value by pulling it from the hook module so a later YAML retune surfaces
# as a test diff rather than a silent failure.
from strategies.configs import v6_sniper as _v6_hooks  # noqa: E402


def test_23_path1_age_uses_inferred_at_when_present():
    """inferred_at populated → _path1_age_s returns real age (NOT the
    surface-assembled-at proxy). Pins the primary signal of this PR:
    a late-window eval that reuses a cached snapshot now sees the
    classifier's actual age, not the assembly drift.

    Direct helper test: exercises the switch logic without the full
    registry-hook path so the assertion is on the helper contract.
    """
    now = time.time()
    # assembled_at 45s ago (what the old proxy would see) vs
    # inferred_at 15s ago (the real classifier age). With max_age=30,
    # the proxy would skip; the new path must NOT skip.
    surface = _make_surface(
        assembled_at=now - 45.0,
        probability_classifier_inferred_at=now - 15.0,
    )
    age_s, source = _v6_hooks._path1_age_s(surface)
    assert source == "inferred_at"
    assert age_s is not None
    assert 14.0 <= age_s <= 17.0, (
        f"expected ~15s from inferred_at, got {age_s}"
    )


def test_24_path1_age_falls_back_to_assembled_at_when_field_absent(registry):
    """inferred_at=None, assembled_at stale, max_age=30 → falls back
    to proxy, gate fires no_eval_blocked. This is the regression
    guard: older surfaces that haven't been repopulated yet continue
    to skip on the proxy instead of silently bypassing the gate.

    The YAML sets ``path1_max_age_s: 90``, so to exercise the 30s
    ceiling called out in the spec we stage a 100s-stale assembled_at
    — comfortably above the YAML value — and assert the gate fires on
    the fallback. That keeps the test independent of YAML tuning: if
    the default is widened to e.g. 120s later, this test still fails
    loudly instead of silently passing by coincidence.
    """
    now = time.time()
    surface = _make_surface(
        assembled_at=now - 100.0,
        probability_classifier_inferred_at=None,
    )
    # Direct helper assertion first — source must be the fallback.
    age_s, source = _v6_hooks._path1_age_s(surface)
    assert source == "assembled_at_fallback"
    assert age_s is not None
    assert 99.0 <= age_s <= 102.0, (
        f"expected ~100s from assembled_at, got {age_s}"
    )

    # Integration assertion: the hook should fire no_eval_blocked with
    # the fallback source attributed in the skip metadata.
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "no_eval_blocked" in (decision.skip_reason or "")
    assert "path1 stale" in (decision.skip_reason or "")
    # Metadata must carry the source tag so audits can attribute the
    # skip to the fallback vs the new per-inference field.
    assert decision.metadata.get("path1_age_source") == "assembled_at_fallback"


def test_25_path1_age_skips_when_inferred_at_exceeds_max():
    """inferred_at very stale (120s), max_age=90 → skip on the REAL
    classifier age, not a proxy. This is the "no more false negatives
    masking true degradation" case: when the upstream classifier
    genuinely stops producing fresh readings, v6 still catches it.
    """
    now = time.time()
    surface = _make_surface(
        assembled_at=now - 5.0,  # surface itself is fresh
        probability_classifier_inferred_at=now - 120.0,  # but classifier IS stale
    )
    # Leave YAML default (90s) — we want the test to fail if the YAML
    # is widened so loosely that genuine 2-min-stale classifiers pass.
    # Direct helper assertion:
    age_s, source = _v6_hooks._path1_age_s(surface)
    assert source == "inferred_at"
    assert age_s is not None
    assert 119.0 <= age_s <= 122.0, (
        f"expected ~120s from inferred_at, got {age_s}"
    )

    # Integration: full hook should SKIP with no_eval_blocked + path1 stale.
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    decision = reg._evaluate_one(
        "v6_sniper", reg.configs["v6_sniper"], surface
    )
    assert decision.action == "SKIP"
    assert "no_eval_blocked" in (decision.skip_reason or "")
    assert "path1 stale" in (decision.skip_reason or "")
    # Skip attributes the staleness to the new inferred_at field,
    # not the fallback — proof that the real per-inference age drove
    # the decision.
    assert decision.metadata.get("path1_age_source") == "inferred_at"
