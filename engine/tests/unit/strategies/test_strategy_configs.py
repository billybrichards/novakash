"""Tests for the 5 production strategy configs -- evaluated against known inputs."""

import sys
import os
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.registry import StrategyRegistry
from strategies.data_surface import DataSurfaceManager, FullDataSurface

# Path to the actual configs directory
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "strategies", "configs")


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        # v4.5.0: bump default vpin into the healthy band (0.50-0.85) so the
        # HealthBadge gate doesn't flag every test surface as DEGRADED via
        # the "vpin:low" amber. Existing tests that want to test vpin=0.45
        # behaviour should override explicitly.
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        # Audit #121 Path 1 ensemble fields (default None — most tests don't care)
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85, v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.38,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.52,
        clob_down_ask=0.54, clob_implied_up=0.47,
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
    return reg


class TestV4DownOnly:
    def test_loads(self, registry):
        assert "v4_down_only" in registry.strategy_names
        assert registry.configs["v4_down_only"].mode == "GHOST"

    def test_trade_down_in_window(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.12, poly_trade_advised=True,
            clob_down_ask=0.60,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_skip_up_direction(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.12, poly_trade_advised=True,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "direction" in decision.skip_reason

    def test_skip_outside_timing(self, registry):
        surface = _make_surface(eval_offset=60)
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "timing" in decision.skip_reason

    def test_skip_low_confidence(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.05, poly_trade_advised=True,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "confidence" in decision.skip_reason


class TestV4UpBasic:
    def test_loads(self, registry):
        assert "v4_up_basic" in registry.strategy_names
        assert registry.configs["v4_up_basic"].mode == "GHOST"

    def test_trade_up_in_window(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.16,  # min_dist raised to 0.15 on 2026-04-14
            v2_probability_up=0.62,
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "UP"

    def test_skip_down(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.12,
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "SKIP"

    def test_wider_timing_than_down(self, registry):
        """v4_up_basic accepts T-65, which v4_down_only rejects."""
        surface = _make_surface(
            eval_offset=65, poly_direction="UP",
            poly_confidence_distance=0.16,  # min_dist raised to 0.15 on 2026-04-14
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "TRADE"


class TestV4UpAsian:
    def test_loads(self, registry):
        assert "v4_up_asian" in registry.strategy_names

    def test_trade_asian_hours(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.15, hour_utc=0,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "TRADE"

    def test_skip_non_asian_hours(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.15, hour_utc=12,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "SKIP"
        assert "session_hours" in decision.skip_reason

    def test_skip_high_confidence(self, registry):
        """max_dist=0.20 should reject dist=0.25."""
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.25, hour_utc=0,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "SKIP"
        assert "confidence" in decision.skip_reason


class TestV4Fusion:
    def test_loads(self, registry):
        assert "v4_fusion" in registry.strategy_names
        assert registry.configs["v4_fusion"].pre_gate_hook == "evaluate_polymarket_v2"

    def test_trade_poly_v2(self, registry):
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            # Chainlink must agree with trade direction (it IS the resolution oracle)
            delta_chainlink=-0.005,
            # v4.4.0: Tiingo gate -- must also agree with trade direction.
            delta_tiingo=-0.004,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_skip_early_timing(self, registry):
        surface = _make_surface(poly_timing="early")
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "timing" in decision.skip_reason

    def test_skip_low_confidence(self, registry):
        surface = _make_surface(
            poly_direction="DOWN", poly_confidence_distance=0.08,
            poly_timing="optimal", poly_trade_advised=True,
            poly_confidence=0.42,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "0.12" in decision.skip_reason

    # ─── v4.3.0: T-45 cutoff + direction-aware risk_off override ────────────

    def test_trade_t45_offset(self, registry):
        """v4.3.0 lowers execution floor from T-70 to T-45 — T-50 should TRADE."""
        surface = _make_surface(
            eval_offset=50,
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal", delta_chainlink=-0.005,
            # v4.4.0: Tiingo gate -- must also agree with trade direction.
            delta_tiingo=-0.004,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_skip_below_t45(self, registry):
        """T-40 still too late even under v4.3.0."""
        surface = _make_surface(
            eval_offset=40,
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "too late" in decision.skip_reason

    def test_risk_off_override_trades_when_aligned(self, registry):
        """HIGH conviction + chainlink aligns → override sister's risk_off veto."""
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,  # sister veto
            poly_confidence=0.72, poly_confidence_distance=0.22,  # HIGH
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",  # from sister
            delta_chainlink=0.005,  # UP rally confirms direction
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "UP"
        assert decision.metadata.get("risk_off_overridden") is True
        assert "override_" in (decision.entry_reason or "")

    def test_risk_off_override_blocked_by_low_conviction(self, registry):
        """MEDIUM conviction (dist<0.20) does NOT unlock override."""
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,
            poly_confidence=0.65, poly_confidence_distance=0.15,  # MEDIUM
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",
            delta_chainlink=0.005,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "risk_off" in decision.skip_reason

    def test_risk_off_override_blocked_by_chainlink_disagree(self, registry):
        """Chainlink points opposite direction → no override."""
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,
            poly_confidence=0.72, poly_confidence_distance=0.22,  # HIGH
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",
            delta_chainlink=-0.005,  # DOWN move vs UP trade
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "risk_off" in decision.skip_reason

    # ─── v4.4.0: CALM regime skip + Tiingo agreement gates ──────────────────

    def test_skip_on_calm_regime(self, registry):
        """VPIN regime=CALM should SKIP with calm_regime_model_underperforms."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            regime="CALM",  # triggers the gate
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert decision.skip_reason == "calm_regime_model_underperforms"
        # Gate trace should record the skip reason
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "calm_regime" and not g["passed"] for g in gates
        )

    def test_trade_on_non_calm_regime(self, registry):
        """VPIN regime=NORMAL passes the calm gate and proceeds to trade."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            regime="NORMAL",
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "calm_regime" and g["passed"] for g in gates
        )

    def test_calm_regime_gate_disabled_via_env(self, registry, monkeypatch):
        """V4_FUSION_SKIP_CALM=false must bypass the CALM skip."""
        monkeypatch.setenv("V4_FUSION_SKIP_CALM", "false")
        # The registry loads hooks via importlib without registering in
        # sys.modules, so we patch the flag directly on the function's
        # __globals__ dict using monkeypatch.setitem (auto-restores).
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(hook_fn.__globals__, "_FUSION_SKIP_CALM", False)

        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            regime="CALM",  # would normally be blocked
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        # The specific gate should NOT be the cause of a skip
        assert decision.skip_reason is None

    def test_skip_on_tiingo_disagrees(self, registry):
        """Tiingo direction opposite to trade → SKIP with tiingo_disagrees."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005,  # chainlink agrees
            delta_tiingo=0.004,       # tiingo UP vs trade DOWN
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "tiingo_disagrees" in decision.skip_reason
        assert "tiingo=UP" in decision.skip_reason
        assert "trade=DOWN" in decision.skip_reason
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "tiingo_agreement" and not g["passed"] for g in gates
        )

    def test_trade_on_tiingo_agrees(self, registry):
        """Tiingo agrees with trade direction → gate passes, trade proceeds."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "tiingo_agreement"
            and g["passed"]
            and "Tiingo agrees" in g["reason"]
            for g in gates
        )

    def test_tiingo_unavailable_passes_gate(self, registry, monkeypatch):
        """With staleness + health gates OFF: missing Tiingo → passes the
        tiingo_agreement gate with 'tiingo_unavailable' (v4.4.0 behavior).

        v4.5.0: both the staleness gate (fires first when tiingo=None) and
        the health gate (sources:unknown → DEGRADED) would otherwise block.
        This test disables both to target tiingo_agreement behavior only.
        See `test_staleness_tiingo_missing_skips` for the v4.5.0 default.
        """
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_SKIP_STALE_SOURCES", False
        )
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off"
        )

        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=None,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "tiingo_agreement"
            and g["passed"]
            and "tiingo_unavailable" in g["reason"]
            for g in gates
        )

    def test_tiingo_gate_disabled_via_env(self, registry, monkeypatch):
        """V4_FUSION_REQUIRE_TIINGO_AGREE=false bypasses the Tiingo check.

        Disables health gate too — the test surface has chainlink and tiingo
        disagreeing which otherwise trips `sources:mixed` amber → DEGRADED
        → health_badge gate blocks. This test targets the tiingo_agreement
        disable behavior specifically.
        """
        monkeypatch.setenv("V4_FUSION_REQUIRE_TIINGO_AGREE", "false")
        # Hook modules are loaded via importlib without registering in
        # sys.modules, so patch via the function's __globals__ with setitem.
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_REQUIRE_TIINGO_AGREE", False
        )
        # v4.5.0: disable health gate so sources_mixed DEGRADED doesn't block
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off"
        )

        # Tiingo would disagree if gate were on
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005,
            delta_tiingo=0.004,  # disagrees with trade direction
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "tiingo_agreement"
            and g["passed"]
            and "tiingo_gate_disabled_by_env" in g["reason"]
            for g in gates
        )

    # ─── v4.5.0: staleness + HealthBadge + override-hardening gates ──────────

    def test_staleness_tiingo_missing_skips(self, registry):
        """V4_FUSION_SKIP_STALE_SOURCES default true — tiingo None → SKIP."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=None,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "feature_stale" in decision.skip_reason
        assert "tiingo" in decision.skip_reason

    def test_staleness_chainlink_missing_skips(self, registry):
        """Chainlink None → SKIP with feature_stale reason."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
            delta_chainlink=None, delta_tiingo=-0.004,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "feature_stale" in decision.skip_reason
        assert "chainlink" in decision.skip_reason

    def test_staleness_gate_disabled_via_env(self, registry, monkeypatch):
        """V4_FUSION_SKIP_STALE_SOURCES=false lets stale sources through."""
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_SKIP_STALE_SOURCES", False
        )
        # Also disable health gate — chainlink+tiingo None → sources_unknown
        # would DEGRADE the badge and block via health gate instead.
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off"
        )
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=None,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        # staleness gate should not appear as a check when disabled (early return)
        assert not any(g["gate"] == "feature_staleness" for g in gates)

    def test_health_badge_degraded_blocks_default(self, registry):
        """Default V4_FUSION_HEALTH_GATE=degraded blocks when a single amber
        flag fires. VPIN<0.40 trips 'vpin:low' → DEGRADED → SKIP."""
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            vpin=0.30,  # below healthy band → amber vpin:low
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "health_degraded" in decision.skip_reason
        assert "vpin:low" in decision.skip_reason

    def test_health_badge_unsafe_mode_allows_degraded(self, registry, monkeypatch):
        """V4_FUSION_HEALTH_GATE=unsafe only blocks UNSAFE, allows DEGRADED."""
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_HEALTH_GATE", "unsafe"
        )
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            vpin=0.30,  # DEGRADED in degraded-mode but NOT UNSAFE
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"

    def test_health_badge_off_ignores_all(self, registry, monkeypatch):
        """V4_FUSION_HEALTH_GATE=off never blocks."""
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off"
        )
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence_distance=0.12, poly_timing="optimal",
            delta_chainlink=-0.005, delta_tiingo=-0.004,
            vpin=0.30,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        gates = decision.metadata.get("gate_results") or []
        assert not any(g["gate"] == "health_badge" for g in gates)

    def test_override_requires_tiingo_when_enabled(self, registry):
        """Risk-off override hardening: tiingo missing → override rejected."""
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,  # sister veto
            poly_confidence=0.72, poly_confidence_distance=0.22,  # HIGH
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",
            delta_chainlink=0.005,  # agrees UP
            delta_tiingo=None,  # but tiingo missing
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        # Staleness gate catches this first actually (tiingo None). Even
        # with staleness off, override would fail on the require-tiingo
        # check. Confirmed via the explicit test below.
        assert decision.action == "SKIP"

    def test_override_rejects_on_tiingo_disagreement(self, registry, monkeypatch):
        """Override hardening: chainlink agrees UP, tiingo disagrees DOWN
        → override rejected even with HIGH conviction."""
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        # Disable staleness + health gate so test targets the override
        # require-tiingo check specifically. Both tiingo & chainlink
        # present, both disagree — sources_agree False → DEGRADED otherwise.
        monkeypatch.setitem(hook_fn.__globals__, "_FUSION_SKIP_STALE_SOURCES", False)
        monkeypatch.setitem(hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off")
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,  # sister veto
            poly_confidence=0.72, poly_confidence_distance=0.22,  # HIGH
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",
            delta_chainlink=0.005,  # agrees UP
            delta_tiingo=-0.004,    # disagrees DOWN
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        gates = decision.metadata.get("gate_results") or []
        assert any(
            g["gate"] == "regime_risk_off_override"
            and not g["passed"]
            and "tiingo" in g["reason"].lower()
            for g in gates
        )

    def test_override_hardening_disabled_via_env(self, registry, monkeypatch):
        """V4_RISK_OFF_OVERRIDE_REQUIRE_TIINGO=false falls back to v4.3.0
        single-source override behaviour (chainlink only)."""
        hook_fn = registry._hooks["v4_fusion"]["evaluate_polymarket_v2"]
        monkeypatch.setitem(
            hook_fn.__globals__, "_RISK_OFF_OVERRIDE_REQUIRE_TIINGO", False
        )
        monkeypatch.setitem(hook_fn.__globals__, "_FUSION_SKIP_STALE_SOURCES", False)
        monkeypatch.setitem(hook_fn.__globals__, "_FUSION_HEALTH_GATE", "off")
        surface = _make_surface(
            eval_offset=120,
            poly_direction="UP", poly_trade_advised=False,
            poly_confidence=0.72, poly_confidence_distance=0.22,
            poly_timing="optimal",
            poly_reason="regime_risk_off_skip",
            delta_chainlink=0.005,  # agrees UP
            delta_tiingo=None,  # missing but should not block when hardening off
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        assert decision.metadata.get("risk_off_overridden") is True


class TestV10Gate:
    def test_loads(self, registry):
        assert "v10_gate" in registry.strategy_names
        assert registry.configs["v10_gate"].post_gate_hook == "classify_confidence"

    def test_trade_all_gates_pass(self, registry):
        surface = _make_surface(
            eval_offset=120,
            delta_tiingo=-0.001, delta_chainlink=-0.001, delta_binance=-0.001,
            delta_pct=-0.001,
            poly_direction="DOWN",
            cg_taker_buy_vol=800, cg_taker_sell_vol=1200,
            poly_confidence_distance=0.15,
            clob_down_bid=0.53, clob_down_ask=0.535,
            v2_probability_up=0.35,
            cg_liq_total=100_000.0,
        )
        decision = registry._evaluate_one(
            "v10_gate", registry.configs["v10_gate"], surface
        )
        assert decision.action == "TRADE"

    def test_skip_low_delta(self, registry):
        surface = _make_surface(
            eval_offset=120, delta_pct=0.0001,
            delta_tiingo=0.0001, delta_chainlink=0.0001,
        )
        decision = registry._evaluate_one(
            "v10_gate", registry.configs["v10_gate"], surface
        )
        assert decision.action == "SKIP"
        assert "delta_magnitude" in decision.skip_reason
