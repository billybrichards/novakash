"""Tests for v9_1_lgb_only_relaxed_dn — DOWN-floor relaxation soak variant.

Coverage:
  1. YAML loads cleanly through StrategyRegistry
  2. Mode is GHOST (NEVER LIVE on initial registration; promotion is manual)
  3. The relaxed DOWN floor (0.15) is in gate_params; UP floor stays strict
     (0.20)
  4. Wrapper hook relabels the StrategyDecision identity to the new
     strategy_id (does NOT leak ``v9_1_lgb_only`` into downstream readers)
  5. SKIP-on-None contract is preserved (delegated through the v9_1_lgb_only
     hook unchanged)
  6. CARVE-OUT — wrapper overrides parent TRADE to SKIP when DOWN dist
     falls in the [0.20, 0.25) bad sub-bucket band

These tests verify the *config + hook glue + carve-out*, not the underlying
gate logic. v9.1 booster + v9_ensemble gate stack are already covered by
``test_v9_lgb_only.py`` and ``test_skip_when_pricetobeat_unavailable.py``.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from domain.value_objects import StrategyDecision
from strategies.configs.v9_1_lgb_only_relaxed_dn import (
    evaluate_v9_1_lgb_only_relaxed_dn,
    _STRATEGY_ID,
    _VERSION,
    _CARVE_OUT_DN_DIST_LO,
    _CARVE_OUT_DN_DIST_HI,
    _CARVE_OUT_SKIP_REASON,
    _down_dist,
)


def _stub_decision(
    *,
    action: str = "TRADE",
    direction: str | None = "DOWN",
    skip_reason: str = "",
) -> StrategyDecision:
    """Build a stub StrategyDecision as if v9_1_lgb_only had returned it."""
    return StrategyDecision(
        action=action,
        direction=direction,
        confidence=0.85,
        confidence_score=0.85,
        entry_cap=0.30,
        collateral_pct=0.025,
        strategy_id="v9_1_lgb_only",  # parent hook stamps this
        strategy_version="9.1.0",
        entry_reason="v9_1_lgb_only:gates_passed" if action == "TRADE" else "",
        skip_reason=skip_reason,
        metadata={
            "probability_lgb_v9_1": 0.32,
            "probability_lgb_prod": 0.40,
            "lgb_only_forced": True,
            "v9_1_active": True,
        },
    )


def _surface_with_prob(p_v9_1: float | None) -> MagicMock:
    """Build a minimal surface mock with `probability_lgb_v9_1` set."""
    surface = MagicMock()
    surface.probability_lgb_v9_1 = p_v9_1
    return surface


class TestStrategyIdRelabel:
    """Wrapper hook MUST relabel decision identity to the new strategy_id,
    otherwise downstream readers see the parent name.
    """

    def test_trade_decision_relabelled(self):
        # dist = 0.18 (relaxed-good zone) — passes through, no carve-out
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE", direction="DOWN"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.32))

        assert decision.strategy_id == _STRATEGY_ID
        assert decision.strategy_id == "v9_1_lgb_only_relaxed_dn"
        assert decision.strategy_version == _VERSION
        assert decision.action == "TRADE"

    def test_skip_decision_relabelled(self):
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(
                action="SKIP",
                direction=None,
                skip_reason="v9_1_model_not_loaded",
            ),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(None))

        # Identity relabelled even on SKIP path
        assert decision.strategy_id == _STRATEGY_ID
        assert decision.action == "SKIP"
        # SKIP reason is preserved verbatim from parent
        assert decision.skip_reason == "v9_1_model_not_loaded"

    def test_entry_reason_relabelled(self):
        """Entry reason should reference new strategy_id, not the parent."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.32))

        assert "v9_1_lgb_only_relaxed_dn" in decision.entry_reason
        assert decision.entry_reason == "v9_1_lgb_only_relaxed_dn:gates_passed"

    def test_metadata_preserved(self):
        """All v9_1 metadata must survive the relabel — auditing depends on it."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.32))

        assert decision.metadata["probability_lgb_v9_1"] == 0.32
        assert decision.metadata["probability_lgb_prod"] == 0.40
        assert decision.metadata["lgb_only_forced"] is True
        assert decision.metadata["v9_1_active"] is True

    def test_metadata_is_a_copy_not_reference(self):
        """Mutating the wrapper's metadata must not corrupt the original."""
        original = _stub_decision(action="TRADE")
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=original,
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.32))

        decision.metadata["mutated"] = "yes"
        assert "mutated" not in original.metadata


class TestCarveOutBadBand:
    """The 0.20-0.25 DOWN dist sub-bucket is the weakest in the entire DOWN
    ladder (65.5% WR n=29). Wrapper MUST override parent TRADE to SKIP for
    decisions in this band — otherwise the soak is contaminated with the
    bad zone we explicitly want to exclude.
    """

    @pytest.mark.parametrize(
        "p_v9_1,expected_dist",
        [
            (0.30, 0.20),  # boundary lower (0.20 <= d, included)
            (0.27, 0.23),  # mid-band
            (0.26, 0.24),  # near upper boundary (still < 0.25, included)
        ],
    )
    def test_carve_out_overrides_parent_trade(self, p_v9_1, expected_dist):
        """Parent TRADE on DOWN with dist in [0.20, 0.25) → wrapper SKIPs."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE", direction="DOWN"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(
                _surface_with_prob(p_v9_1)
            )

        assert decision.action == "SKIP"
        assert decision.direction is None
        assert decision.skip_reason == _CARVE_OUT_SKIP_REASON
        assert decision.strategy_id == _STRATEGY_ID
        # Soak metadata for analysis
        assert decision.metadata["carve_out_applied"] is True
        assert decision.metadata["carve_out_band"] == "[0.2, 0.25)"
        assert abs(decision.metadata["carve_out_dist"] - expected_dist) < 1e-9
        assert decision.metadata["parent_action"] == "TRADE"

    @pytest.mark.parametrize(
        "p_v9_1",
        [
            0.34,  # dist=0.16 — relaxed-good zone (NOT carved out)
            0.31,  # dist=0.19 — relaxed-good zone (NOT carved out)
            0.25,  # dist=0.25 — high-conviction (NOT carved out, falls into prod overlap zone)
            0.20,  # dist=0.30 — high-conviction
            0.10,  # dist=0.40 — extreme conviction
        ],
    )
    def test_no_carve_out_outside_band(self, p_v9_1):
        """DOWN trades OUTSIDE the [0.20, 0.25) band must pass through."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE", direction="DOWN"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(
                _surface_with_prob(p_v9_1)
            )

        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"
        assert decision.skip_reason != _CARVE_OUT_SKIP_REASON
        # carve_out_applied flag NOT set on pass-through
        assert "carve_out_applied" not in decision.metadata

    def test_carve_out_does_not_apply_to_up_trades(self):
        """Carve-out is DOWN-only. UP trades pass through regardless of dist."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE", direction="UP"),
        ):
            # p=0.72 → UP dist=0.22 (would be in carve-out band IF this were DOWN)
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.72))

        assert decision.action == "TRADE"
        assert decision.direction == "UP"
        assert decision.skip_reason != _CARVE_OUT_SKIP_REASON

    def test_carve_out_does_not_apply_when_parent_skips(self):
        """If parent SKIPped (any reason), carve-out is moot — wrapper passes
        through the SKIP unchanged (just relabels)."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(
                action="SKIP",
                direction=None,
                skip_reason="oracle_disagree",
            ),
        ):
            # p=0.27 — would be in carve-out band IF parent had returned TRADE-DOWN
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(0.27))

        assert decision.action == "SKIP"
        # Parent's skip_reason preserved (NOT overwritten by carve_out reason)
        assert decision.skip_reason == "oracle_disagree"

    def test_carve_out_handles_none_probability(self):
        """If probability_lgb_v9_1 is None on surface, the parent should have
        already SKIPped with v9_1_model_not_loaded. Wrapper passes that through.
        """
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(
                action="SKIP",
                direction=None,
                skip_reason="v9_1_model_not_loaded",
            ),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(_surface_with_prob(None))

        assert decision.action == "SKIP"
        assert decision.skip_reason == "v9_1_model_not_loaded"


class TestDownDistHelper:
    """Unit tests for the _down_dist helper — distance-below-coinflip."""

    def test_returns_none_when_prob_is_none(self):
        assert _down_dist(None) is None

    def test_returns_none_when_prob_indicates_up(self):
        # Probability >= 0.50 means UP candidate — DOWN dist not applicable
        assert _down_dist(0.50) is None
        assert _down_dist(0.60) is None
        assert _down_dist(0.99) is None

    def test_computes_dist_correctly_for_down(self):
        assert _down_dist(0.30) == pytest.approx(0.20)
        assert _down_dist(0.25) == pytest.approx(0.25)
        assert _down_dist(0.10) == pytest.approx(0.40)
        assert _down_dist(0.00) == pytest.approx(0.50)


class TestStrategyConstants:
    """Hard-pin constants — these must match the YAML name exactly."""

    def test_strategy_id_matches_yaml_name(self):
        assert _STRATEGY_ID == "v9_1_lgb_only_relaxed_dn"

    def test_version_includes_relaxed_dn_suffix(self):
        assert "relaxed-dn" in _VERSION
        assert _VERSION == "9.1.0-relaxed-dn"

    def test_carve_out_band_is_correct(self):
        """Carve-out is exactly [0.20, 0.25) — half-open, includes 0.20,
        excludes 0.25 (so dist=0.25 falls into production-overlap zone).
        """
        assert _CARVE_OUT_DN_DIST_LO == 0.20
        assert _CARVE_OUT_DN_DIST_HI == 0.25

    def test_carve_out_skip_reason_is_unique(self):
        """The skip_reason string must be unique so soak analysis can grep
        for carve-out events distinctly from other SKIP causes."""
        assert "carve_out" in _CARVE_OUT_SKIP_REASON
        assert "0.20" in _CARVE_OUT_SKIP_REASON
        assert "0.25" in _CARVE_OUT_SKIP_REASON


class TestRegistryLoad:
    """The YAML must auto-discover via StrategyRegistry, with GHOST mode
    and the relaxed DOWN floor.
    """

    @pytest.fixture
    def registry(self):
        from strategies.registry import StrategyRegistry

        config_dir = Path(__file__).resolve().parents[3] / "strategies" / "configs"
        reg = StrategyRegistry(config_dir=config_dir)
        reg.load_all()
        return reg

    def test_loads(self, registry):
        assert "v9_1_lgb_only_relaxed_dn" in registry.strategy_names

    def test_mode_is_ghost(self, registry):
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        assert config.mode == "GHOST", (
            "Initial registration MUST be GHOST. Promotion to LIVE happens "
            "via Hub API + strategy_runtime_overrides table — never the YAML."
        )

    def test_down_floor_relaxed(self, registry):
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        # DOWN floor is the WHOLE POINT of this variant — relaxed to 0.15
        assert config.gate_params["lgb_dist_min_down"] == 0.15

    def test_up_floor_strict(self, registry):
        """UP relaxation rejected per Wilson analysis (Hub note #342)."""
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        assert config.gate_params["lgb_dist_min_up"] == 0.20

    def test_down_relaxed_below_production(self, registry):
        """The DOWN floor MUST be lower than v9_1_lgb_only's production
        runtime override (0.25), otherwise this variant has no purpose.
        """
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        production_runtime_override = 0.25
        assert config.gate_params["lgb_dist_min_down"] < production_runtime_override

    def test_hooks_file_points_to_wrapper(self, registry):
        """YAML must load the wrapper hook so the carve-out runs."""
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        assert config.hooks_file == "v9_1_lgb_only_relaxed_dn.py"
        assert config.pre_gate_hook == "evaluate_v9_1_lgb_only_relaxed_dn"

    def test_sizing_half_stake(self, registry):
        """Sizing is half-stake of v9_1_lgb_only LIVE (0.25 → 0.125)."""
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        sizing = config.sizing
        fraction = (
            sizing.get("fraction")
            if isinstance(sizing, dict)
            else getattr(sizing, "fraction", None)
        )
        assert fraction == 0.125
