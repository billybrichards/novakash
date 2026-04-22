"""Tests for v8_champion — new primary LIVE strategy.

Covers:

  Registry load sanity (LIVE + version + timescale)
  1. Timing bounds (inside / below min / above max)
  2. UTC hour block (0, 5, 14 → SKIP; allowed hour → pass)
  3. Regime allowlist (volatile_trend, chop accept; calm_trend, risk_off skip)
  4. Source agreement null-block (chainlink None / tiingo None)
  5. VPIN gate (below floor, above ceiling, inside)
  6. Ensemble bucket (agree_strong, pegged_path1 accept; mid_conf skip; no_eval skip)
  7. Fill-band (below, above, inside)
  8. UP asymmetric gates (UP fill < 0.55 → SKIP; UP needs both buckets; DOWN unaffected)
  9. Full accept path emits TRADE with expected metadata shape

Strategy hooks live in ``engine/strategies/configs/v8_champion.py`` and are
loaded via importlib by ``StrategyRegistry``. Exercised through the registry
end-to-end, same pattern as test_v5_ensemble and test_v6_sniper.
"""

from __future__ import annotations

import datetime as _dt
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


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface landing inside v8_champion's accept window (DOWN path).

    Direction=DOWN, pegged path1 (p_path1=0.08), dist=0.25 → agree_strong AND
    pegged_path1 buckets both fire. UTC hour=12 (allowed). VPIN=0.55 (inside
    [0.40, 0.85]). Chainlink + Tiingo both present. CLOB down ask=0.55
    (inside [0.30, 0.65]). Regime=volatile_trend (tradeable).
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,  # 2024-04-13 12:00 UTC
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.25, v2_probability_raw=0.25,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=0.08,
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
    """Symmetric UP-direction surface matching v8's strictest accept path.

    UP requires BOTH agree_strong AND pegged_path1. Put p_path1=0.96 (extreme
    UP) with p_up=0.75 (dist=0.25 ≥ 0.20 strong threshold) and LGB=0.80
    (aligned UP). CLOB up ask=0.60 (above UP floor 0.55).
    """
    defaults = dict(
        poly_direction="UP", poly_confidence=0.75,
        poly_confidence_distance=0.25,
        probability_lgb=0.80, probability_classifier=0.96,
        delta_binance=+0.005, delta_tiingo=+0.004, delta_chainlink=+0.005,
        v4_recommended_side="UP", v2_probability_up=0.75,
        clob_up_bid=0.58, clob_up_ask=0.60,
        clob_down_bid=0.38, clob_down_ask=0.40,
        gamma_up_price=0.60, gamma_down_price=0.40,
    )
    defaults.update(overrides)
    return _make_surface(**defaults)


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v8_champion", registry.configs["v8_champion"], surface
    )


# ── Registry load sanity ────────────────────────────────────────────────────
def test_registered_as_live(registry):
    assert "v8_champion" in registry.strategy_names
    cfg = registry.configs["v8_champion"]
    assert cfg.mode == "LIVE"
    assert cfg.version == "8.0.0"
    assert cfg.timescale == "5m"
    assert cfg.asset == "BTC"
    # Key gate_params present.
    gp = cfg.gate_params
    for key in (
        "min_offset_sec",
        "max_offset_sec",
        "tradeable_v4_regimes",
        "bucket_abs_dist_strong",
        "bucket_path1_extreme_high",
        "bucket_path1_extreme_low",
        "fill_band_min",
        "fill_band_max",
        "up_min_fill_price",
        "up_require_both_buckets",
        "blocked_utc_hours",
        "source_agreement_require_chainlink",
        "source_agreement_require_tiingo",
        "vpin_min",
        "vpin_max",
    ):
        assert key in gp, f"missing gate_param: {key}"
    assert gp["up_min_fill_price"] == 0.55
    assert gp["blocked_utc_hours"] == [0, 5, 14]
    assert set(gp["tradeable_v4_regimes"]) == {"volatile_trend", "chop"}


# ── 1. Timing ──────────────────────────────────────────────────────────────
def test_timing_inside_window_accepts(registry):
    surface = _make_surface(eval_offset=120)
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason


def test_timing_below_min_skips(registry):
    surface = _make_surface(eval_offset=10)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "timing" in d.skip_reason


def test_timing_above_max_skips(registry):
    surface = _make_surface(eval_offset=250)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "timing" in d.skip_reason


# ── 2. Hour block ──────────────────────────────────────────────────────────
def _ts_for_hour(hour: int) -> int:
    return int(
        _dt.datetime(2024, 4, 13, hour, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    )


def test_hour_block_h0_skips(registry):
    surface = _make_surface(window_ts=_ts_for_hour(0))
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "utc_hour_block" in d.skip_reason


def test_hour_block_h5_skips(registry):
    surface = _make_surface(window_ts=_ts_for_hour(5))
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "utc_hour_block" in d.skip_reason


def test_hour_block_h14_skips(registry):
    surface = _make_surface(window_ts=_ts_for_hour(14))
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "utc_hour_block" in d.skip_reason


def test_hour_block_h12_allowed(registry):
    surface = _make_surface(window_ts=_ts_for_hour(12))
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason


# ── 3. Regime allowlist ────────────────────────────────────────────────────
def test_regime_volatile_trend_accepts(registry):
    surface = _make_surface(v4_regime="volatile_trend")
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason


def test_regime_chop_accepts(registry):
    surface = _make_surface(v4_regime="chop")
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason


def test_regime_calm_trend_skips(registry):
    surface = _make_surface(v4_regime="calm_trend")
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "v4_regime" in d.skip_reason


def test_regime_risk_off_skips(registry):
    surface = _make_surface(v4_regime="risk_off")
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "v4_regime" in d.skip_reason


def test_regime_none_skips(registry):
    surface = _make_surface(v4_regime=None)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"


# ── 4. Source agreement ────────────────────────────────────────────────────
def test_chainlink_missing_skips(registry):
    surface = _make_surface(delta_chainlink=None)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "source_agreement" in d.skip_reason


def test_tiingo_missing_skips(registry):
    surface = _make_surface(delta_tiingo=None)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "source_agreement" in d.skip_reason


# ── 5. VPIN gate ───────────────────────────────────────────────────────────
def test_vpin_below_floor_skips(registry):
    surface = _make_surface(vpin=0.30)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "vpin" in d.skip_reason


def test_vpin_above_ceiling_skips(registry):
    surface = _make_surface(vpin=0.90)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "vpin" in d.skip_reason


def test_vpin_inside_accepts(registry):
    surface = _make_surface(vpin=0.55)
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason


# ── 6. Ensemble bucket ────────────────────────────────────────────────────
def test_bucket_pegged_path1_accepts(registry):
    # Default DOWN surface has p_path1=0.08 (pegged).
    surface = _make_surface()
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason
    assert d.metadata["conviction_bucket"] in (
        "pegged_path1",
        "agree_strong_and_pegged",
    )


def test_bucket_agree_strong_no_peg_accepts(registry):
    """DOWN with dist=0.25 + LGB aligned DOWN + path1 mid (0.20) → agree_strong."""
    surface = _make_surface(
        probability_lgb=0.30,
        probability_classifier=0.20,  # NOT pegged
        poly_confidence=0.25,
        v2_probability_up=0.25,
    )
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason
    assert d.metadata["conviction_bucket"] == "agree_strong"


def test_bucket_mid_conf_skips(registry):
    """Neither agree_strong nor pegged → mid_conf SKIP."""
    surface = _make_surface(
        probability_lgb=0.48,
        probability_classifier=0.52,  # mid
        poly_confidence=0.52,
        poly_confidence_distance=0.02,
        v2_probability_up=0.52,
        poly_direction="UP",
    )
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "ensemble_bucket" in d.skip_reason


def test_bucket_no_eval_skips(registry):
    surface = _make_surface(probability_classifier=None)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"


# ── 7. Fill-band ──────────────────────────────────────────────────────────
def test_fill_below_band_skips(registry):
    surface = _make_surface(clob_down_ask=0.20, poly_max_entry_price=0.20)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "fill_band" in d.skip_reason


def test_fill_above_band_skips(registry):
    surface = _make_surface(clob_down_ask=0.80, poly_max_entry_price=0.80)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "fill_band" in d.skip_reason


# ── 8. UP asymmetric ──────────────────────────────────────────────────────
def test_up_accept_at_fill_060(registry):
    surface = _up_surface(clob_up_ask=0.60)
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "UP"
    assert d.metadata["fill_price"] == pytest.approx(0.60)


def test_up_rejected_below_0_55_fill(registry):
    surface = _up_surface(clob_up_ask=0.45, poly_max_entry_price=0.45)
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    # fill_band is [0.30, 0.65] so 0.45 passes that — UP fill-floor is what rejects.
    assert (
        "up_fill_below_min" in d.skip_reason
        or "up_fill_floor" in d.skip_reason
    )


def test_up_requires_both_buckets(registry):
    """UP with only agree_strong (path1 not pegged) must SKIP."""
    surface = _up_surface(
        probability_lgb=0.80,
        probability_classifier=0.60,  # NOT pegged (below 0.90)
        poly_confidence=0.75,
    )
    d = _evaluate(registry, surface)
    assert d.action == "SKIP"
    assert "up_require_both_buckets" in d.skip_reason or "up_both_buckets" in (
        d.skip_reason or ""
    )


def test_down_does_not_require_both_buckets(registry):
    """DOWN with only agree_strong (path1 mid) must still TRADE."""
    surface = _make_surface(
        probability_lgb=0.30,
        probability_classifier=0.20,  # NOT pegged (above 0.10)
        poly_confidence=0.25,
        v2_probability_up=0.25,
    )
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason
    assert d.direction == "DOWN"


# ── 9. Full accept path metadata ──────────────────────────────────────────
def test_trade_metadata_shape(registry):
    surface = _make_surface()
    d = _evaluate(registry, surface)
    assert d.action == "TRADE", d.skip_reason
    m = d.metadata
    # Required metadata keys
    for key in (
        "gate_results",
        "poly_direction",
        "conviction_bucket",
        "is_agree_strong",
        "is_pegged_path1",
        "probability_lgb",
        "probability_classifier",
        "probability_used",
        "fill_price",
        "chainlink_delta",
        "tiingo_delta",
        "v4_regime",
        "vpin",
    ):
        assert key in m, f"missing metadata key: {key}"
    assert d.entry_cap == 0.80
    assert d.confidence == "HIGH"
    # gate_results should include each gate we traversed
    names = {g["gate"] for g in m["gate_results"]}
    assert "timing" in names
    assert "v4_regime" in names
    assert "ensemble_bucket" in names
    assert "fill_band" in names
