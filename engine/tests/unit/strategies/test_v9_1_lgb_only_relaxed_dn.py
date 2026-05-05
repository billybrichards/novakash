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

These tests verify the *config + hook glue*, not the underlying gate logic.
v9.1 booster + v9_ensemble gate stack are already covered by
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


class TestStrategyIdRelabel:
    """Wrapper hook MUST relabel the decision identity to the new
    strategy_id, otherwise downstream readers see the parent name.
    """

    def test_trade_decision_relabelled(self):
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE", direction="DOWN"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(MagicMock())

        assert decision.strategy_id == _STRATEGY_ID
        assert decision.strategy_id == "v9_1_lgb_only_relaxed_dn"
        assert decision.strategy_version == _VERSION
        assert decision.strategy_version == "9.1.0-relaxed-dn"

    def test_skip_decision_relabelled(self):
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(
                action="SKIP",
                direction=None,
                skip_reason="v9_1_model_not_loaded",
            ),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(MagicMock())

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
            decision = evaluate_v9_1_lgb_only_relaxed_dn(MagicMock())

        assert "v9_1_lgb_only_relaxed_dn" in decision.entry_reason
        assert decision.entry_reason == "v9_1_lgb_only_relaxed_dn:gates_passed"

    def test_metadata_preserved(self):
        """All v9_1 metadata must survive the relabel — auditing depends on it."""
        with patch(
            "strategies.configs.v9_1_lgb_only_relaxed_dn._evaluate_v9_1",
            return_value=_stub_decision(action="TRADE"),
        ):
            decision = evaluate_v9_1_lgb_only_relaxed_dn(MagicMock())

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
            decision = evaluate_v9_1_lgb_only_relaxed_dn(MagicMock())

        decision.metadata["mutated"] = "yes"
        # Original metadata dict should not be polluted
        assert "mutated" not in original.metadata


class TestStrategyConstants:
    """Hard-pin the strategy_id and version constants — these must match
    the YAML name exactly, otherwise the registry will register the YAML
    under one name and the decision rows will be stamped with another.
    """

    def test_strategy_id_matches_yaml_name(self):
        assert _STRATEGY_ID == "v9_1_lgb_only_relaxed_dn"

    def test_version_includes_relaxed_dn_suffix(self):
        assert "relaxed-dn" in _VERSION
        assert _VERSION == "9.1.0-relaxed-dn"


class TestRegistryLoad:
    """The YAML must auto-discover via StrategyRegistry, and the loaded
    config must (a) have GHOST mode and (b) the relaxed DOWN floor.
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
        # DOWN floor is the WHOLE POINT of this variant
        assert config.gate_params["lgb_dist_min_down"] == 0.15

    def test_up_floor_strict(self, registry):
        """UP relaxation was rejected per Wilson analysis (Hub note #342).
        Variant must keep UP at the production strict floor.
        """
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
        """YAML must load the wrapper hook, NOT the parent — otherwise
        decisions are stamped with the wrong strategy_id by the hook.
        """
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        assert config.hooks_file == "v9_1_lgb_only_relaxed_dn.py"
        assert config.pre_gate_hook == "evaluate_v9_1_lgb_only_relaxed_dn"

    def test_sizing_half_stake(self, registry):
        """Sizing block must be set to half-stake (fraction=0.125 = half
        of v9_1_lgb_only LIVE 0.25). Only relevant when promoted to LIVE,
        but it's the documented contract.
        """
        config = registry.configs["v9_1_lgb_only_relaxed_dn"]
        # Sizing is parsed by the registry into a dict
        sizing = config.sizing
        # sizing may be a dict or a dataclass — handle both
        fraction = (
            sizing.get("fraction")
            if isinstance(sizing, dict)
            else getattr(sizing, "fraction", None)
        )
        assert fraction == 0.125
