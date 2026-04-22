"""Tests for v6_sniper v6.1.1 — direction-asymmetric gating (stricter UP).

Data-driven from live Montreal observations today (2026-04-20):
  * v6 DOWN: 83% WR (10W/2L), +$24.27, ROI +68.6%.
  * v6 UP:   62% WR (5W/3L),  +$4.65,  ROI +17.9%.
  * 2 consecutive UP losses 21:02 + 21:06 (YES @ 0.84 / 0.83) triggered
    the asymmetric tightening.

Spec coverage (11 tests):
  1.  test_up_requires_both_buckets              — UP + agree_strong only → SKIP
  2.  test_up_allowed_when_both_buckets          — UP + both buckets satisfied → TRADE
  3.  test_down_allows_single_bucket             — DOWN + agree_strong only → TRADE
  4.  test_up_higher_dist_threshold              — UP + dist=0.25 < 0.28 → agree_strong FALSE
  5.  test_down_keeps_022_threshold              — DOWN + dist=0.25 >= 0.22 → agree_strong TRUE
  6.  test_up_peg_stricter                       — UP + path1=0.93 < 0.95 → pegged FALSE
  7.  test_down_peg_unchanged                    — DOWN + path1=0.08 <= 0.08 → pegged TRUE
  8.  test_up_entry_cap_075                      — UP decision carries entry_cap=0.75
  9.  test_down_entry_cap_085                    — DOWN decision carries entry_cap=0.85
  10. test_version_bumped                        — YAML version == "6.1.1"
  11. test_risk_off_override_still_false         — regression guard (PR #310 merge order)
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


def _make_surface(**overrides) -> FullDataSurface:
    """Base surface: default DOWN direction, high-fill, agree_strong bucket.

    Subclasses flip just the knob they're testing. Default fill (0.65) is
    above mid_range_fill_max (0.60) so mid-range gate never trips unless a
    test explicitly lowers the ask.
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
    """UP-direction surface: both oracles positive so source_agreement passes.

    Default has |dist| = 0.30 (≥ up_bucket_abs_dist_strong 0.28), path1 not
    pegged (0.78), agree_strong True / pegged_path1 False. Tests flip knobs.
    """
    up_defaults = dict(
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005,
        poly_direction="UP", poly_confidence=0.80,
        poly_confidence_distance=0.30,
        probability_lgb=0.70, probability_classifier=0.78,
        v4_macro_bias="BULL",
        v4_recommended_side="UP",
        clob_up_bid=0.63, clob_up_ask=0.65,
        clob_down_bid=0.33, clob_down_ask=0.35,
        clob_implied_up=0.66,
        gamma_up_price=0.65, gamma_down_price=0.35,
    )
    up_defaults.update(overrides)
    return _make_surface(**up_defaults)


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


# ── Test 1 ──────────────────────────────────────────────────────────────────
def test_up_requires_both_buckets(registry):
    """UP direction + agree_strong only (dist=0.35, path1=0.78) → SKIP.

    With up_require_both_buckets=true, UP needs BOTH conditions.
    v6.1.2 update: raised poly_confidence 0.80→0.85 and lgb 0.70→0.75 so the
    surface clears v6.1.2's Tier-1 gates (min_confidence_score 0.70, lgb_aligned_up_min
    0.70, head agreement < 0.30); the direction_asym_up SKIP is still the one under test.
    """
    surface = _make_up_surface(
        poly_confidence=0.85, poly_confidence_distance=0.35,
        probability_lgb=0.75, probability_classifier=0.78,
        # High-fill band so mid-range gate doesn't short-circuit.
        clob_up_ask=0.65, clob_down_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # v6.1.3: UP blocked at the bucket stage (mid_conf_blocked) because the
    # emergency up_bucket_* thresholds (1.01) make `pegged_path1` and
    # `agree_strong` unreachable for UP. Under v6.1.1 this same surface was
    # blocked at the direction_asym_up gate instead — both are UP-blocking
    # paths. The direction_asym_up gate is checked AFTER the bucket gate,
    # so it is not reached once the bucket is rejected. Either skip reason
    # is acceptable evidence that UP is refused.
    assert any(
        token in (decision.skip_reason or "")
        for token in ("direction_asym_up", "mid_conf_blocked")
    ), f"expected UP-refused skip reason, got {decision.skip_reason}"


# ── Test 2 ──────────────────────────────────────────────────────────────────
def test_up_allowed_when_both_buckets(registry):
    """v6.1.3 EMERGENCY DOWN-only: UP can no longer satisfy the double-bucket
    even with the strongest possible signal (dist=0.46, path1=0.96) because
    up_bucket_abs_dist_strong (1.01) and up_bucket_path1_extreme_high (1.01)
    are now impossible thresholds. The surface that USED to TRADE under
    v6.1.1/v6.1.2 now SKIPs — that's the intended behaviour of the
    emergency block.

    Historical behaviour (v6.1.1, v6.1.2): this surface returned TRADE.
    To restore: revert up_bucket_abs_dist_strong → 0.28 and
    up_bucket_path1_extreme_high → 0.95 in v6_sniper.yaml.
    """
    surface = _make_up_surface(
        poly_confidence=0.96, poly_confidence_distance=0.46,
        probability_lgb=0.90, probability_classifier=0.96,
        # High-fill band to bypass mid-range gate entirely.
        clob_up_ask=0.65, clob_down_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"v6.1.3 emergency: expected SKIP (UP blocked), got {decision.action} "
        f"skip_reason={decision.skip_reason}"
    )
    # v6.1.3: UP blocks at the bucket stage (mid_conf_blocked). Historical
    # (v6.1.1/v6.1.2) would SKIP here too but via direction_asym_up gate.
    # Accept either UP-refused skip reason.
    assert any(
        token in (decision.skip_reason or "")
        for token in ("direction_asym_up", "mid_conf_blocked")
    ), f"expected UP-refused skip reason, got {decision.skip_reason}"


# ── Test 3 ──────────────────────────────────────────────────────────────────
def test_down_allows_single_bucket(registry):
    """DOWN + agree_strong only (dist=0.35, path1=0.20 non-pegged) → TRADE.

    DOWN direction keeps v6.1.0 semantics: either bucket alone accepts.
    v6.1.2 update: lowered poly_confidence 0.25→0.15 and lgb 0.30→0.20 so the
    surface clears Tier-1 gates (confscore floor 0.70 and lgb_aligned_down_max 0.30);
    agree_strong / single-bucket acceptance on DOWN is still what's under test.
    """
    surface = _make_surface(
        poly_direction="DOWN",
        poly_confidence=0.15, poly_confidence_distance=0.35,
        probability_lgb=0.20, probability_classifier=0.20,
        clob_down_ask=0.65, clob_up_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # DOWN direction must NOT hit the direction_asym_up gate at all.
    assert _gate_result(decision, "direction_asym_up") is None


# ── Test 4 ──────────────────────────────────────────────────────────────────
def test_up_higher_dist_threshold(registry):
    """UP + dist=0.25 (below new UP threshold 0.28) → agree_strong FALSE.

    Bucket falls through to mid_conf and gets blocked. The gate-level signal
    is that direction_asym_up never fires (because bucket is not in the
    accept set in the first place — the conviction_bucket gate rejects).
    """
    surface = _make_up_surface(
        poly_confidence=0.75, poly_confidence_distance=0.25,
        probability_lgb=0.70, probability_classifier=0.75,
        clob_up_ask=0.65, clob_down_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", decision.skip_reason
    # Bucket should be mid_conf (dist 0.25 < up thr 0.28, path1 0.75 not pegged).
    bucket = decision.metadata.get("conviction_bucket")
    assert bucket in ("mid_conf_blocked",), f"got bucket={bucket}"
    assert "mid_conf_blocked" in (decision.skip_reason or "")


# ── Test 5 ──────────────────────────────────────────────────────────────────
def test_down_keeps_022_threshold(registry):
    """DOWN + dist=0.35 still buckets as agree_strong using shared threshold.

    Confirms DOWN uses the shared (v6.1.0) threshold path, not the UP-only
    override. v6.1.2 update: raised dist 0.25→0.35 and lowered lgb 0.30→0.20
    so Tier-1 gates (confscore floor 0.70, lgb_aligned_down_max 0.30) pass;
    the direction-asymmetric bucket-classification branch is still under test.
    """
    surface = _make_surface(
        poly_direction="DOWN",
        poly_confidence=0.15, poly_confidence_distance=0.35,
        probability_lgb=0.20, probability_classifier=0.20,
        clob_down_ask=0.65, clob_up_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", decision.skip_reason
    assert decision.metadata.get("conviction_bucket") == "agree_strong"


# ── Test 6 ──────────────────────────────────────────────────────────────────
def test_up_peg_stricter(registry):
    """UP + path1=0.93 (below new up_peg 0.95) + weak dist → pegged FALSE.

    With UP requiring 0.95 to peg, path1=0.93 no longer triggers pegged_path1.
    Combined with a sub-threshold dist, classification falls to mid_conf.
    """
    surface = _make_up_surface(
        # |dist| = 0.20 < up_bucket_abs_dist_strong 0.28 → NOT agree_strong
        poly_confidence=0.70, poly_confidence_distance=0.20,
        probability_lgb=0.55, probability_classifier=0.93,
        clob_up_ask=0.70, clob_down_ask=0.30,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", decision.skip_reason
    # Should bucket as mid_conf (not pegged, not agree_strong).
    assert decision.metadata.get("conviction_bucket") == "mid_conf_blocked"


# ── Test 7 ──────────────────────────────────────────────────────────────────
def test_down_peg_unchanged(registry):
    """DOWN + path1=0.08 → pegged_path1 TRUE (unchanged v6.1.0 behaviour).

    DOWN uses the low-side threshold (0.08) which is not touched by v6.1.1.
    """
    surface = _make_surface(
        poly_direction="DOWN",
        poly_confidence=0.08, poly_confidence_distance=0.42,
        probability_lgb=0.10, probability_classifier=0.08,
        clob_down_ask=0.92, clob_up_ask=0.08,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", decision.skip_reason
    assert decision.metadata.get("conviction_bucket") == "pegged_path1"


# ── Test 8 ──────────────────────────────────────────────────────────────────
def test_up_entry_cap_075(registry):
    """v6.1.3 EMERGENCY DOWN-only: UP can no longer TRADE so entry_cap is
    never exercised on UP. Prior to v6.1.3 this test asserted
    ``decision.entry_cap == 0.75``; that assertion is now moot because UP
    always SKIPs. We keep the test as a regression guard for the SKIP path.

    To re-enable the historical assertion: revert the emergency UP threshold
    bumps in v6_sniper.yaml (1.01 → 0.28 / 0.95) and restore the original
    assert body.
    """
    surface = _make_up_surface(
        poly_confidence=0.96, poly_confidence_distance=0.46,
        probability_lgb=0.90, probability_classifier=0.96,
        clob_up_ask=0.65, clob_down_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"v6.1.3 emergency: UP must SKIP; got {decision.action} "
        f"skip_reason={decision.skip_reason}"
    )
    # v6.1.3: bucket rejects UP first (mid_conf_blocked). Accept either the
    # bucket-stage rejection or the later direction_asym_up gate.
    assert any(
        token in (decision.skip_reason or "")
        for token in ("direction_asym_up", "mid_conf_blocked")
    ), f"expected UP-refused skip reason, got {decision.skip_reason}"


# ── Test 9 ──────────────────────────────────────────────────────────────────
def test_down_entry_cap_085(registry):
    """DOWN decision carries entry_cap = 0.85 (v6.0.1 shared override).

    DOWN is unaffected by v6.1.1's up_entry_cap_override.
    v6.1.2 update: same surface tuning as test_down_allows_single_bucket to
    clear Tier-1 gates; entry_cap semantics are unchanged and still under test.
    """
    surface = _make_surface(
        poly_direction="DOWN",
        poly_confidence=0.15, poly_confidence_distance=0.35,
        probability_lgb=0.20, probability_classifier=0.20,
        clob_down_ask=0.65, clob_up_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", decision.skip_reason
    assert decision.entry_cap == pytest.approx(0.85), (
        f"expected DOWN entry_cap=0.85, got {decision.entry_cap}"
    )


# ── Test 10 ─────────────────────────────────────────────────────────────────
def test_version_at_least_611():
    """YAML version must be >= 6.1.1 (forward-compat: 6.1.2, 6.2.0, ...).

    Originally pinned to '6.1.1' exactly; relaxed in v6.1.2 so subsequent
    minor bumps don't unseat this file's direction-asymmetric regression
    coverage. A dedicated ``test_version_bumped`` lives in the v6.1.2
    test file for the exact-version pin.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    version = data["version"]
    parts = tuple(int(x) for x in str(version).split("."))
    assert parts >= (6, 1, 1), f"expected version >= 6.1.1, got {version}"


# ── Test 11 ─────────────────────────────────────────────────────────────────
def test_risk_off_override_still_false():
    """Regression guard: risk_off_override_enabled must stay False.

    v6.1.1's YAML absorbs PR #310's flip. If a future merge/rebase reintroduces
    the flag as True we want this test to fail loudly before deploy.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["risk_off_override_enabled"] is False, (
        f"risk_off_override_enabled must be False (Montreal hotfix), "
        f"got {data['gate_params']['risk_off_override_enabled']}"
    )


# ── Test 12 ─────────────────────────────────────────────────────────────────
def test_tradeable_v4_regimes_volatile_only():
    """Montreal parity (2026-04-21): v6 restricted to volatile_trend only.

    Live Montreal data: volatile_trend 69% WR vs calm/risk_off 42-47% WR.
    Manually patched on Montreal prod at 17:47 UTC. This guard prevents a
    merge/rebase from reintroducing calm_trend or risk_off.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["tradeable_v4_regimes"] == ["volatile_trend"], (
        f"tradeable_v4_regimes must be exactly ['volatile_trend'] "
        f"(Montreal parity), got {data['gate_params']['tradeable_v4_regimes']}"
    )


# ── Test 13 ─────────────────────────────────────────────────────────────────
def test_max_offset_sec_montreal_parity():
    """Montreal parity: max_offset_sec pinned to 200.

    Montreal prod runs 200; develop had drifted to 240. Pin both to 200 to
    keep live + repo in sync.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["gate_params"]["max_offset_sec"] == 200, (
        f"max_offset_sec must be 200 (Montreal parity), "
        f"got {data['gate_params']['max_offset_sec']}"
    )
