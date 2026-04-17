"""Tests for DecisionMetadata VO + StrategyDecision factory methods.

Phase 3a of the Three Builders convergence. Covers:

  * VO invariants (frozen, validation)
  * Factory constructors (empty, from_dict, from_surface pattern)
  * Legacy-key fallback (v4_regime, v4_conviction)
  * window_ts=0 vs None distinction (the bug the VO was designed to prevent)
  * to_dict() serialisation shape
  * with_extras() immutable merge
  * StrategyDecision.trade()/skip()/error() factories
  * _coerce_metadata shim (dict / VO / None acceptance)
"""

import sys
import os
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from domain.decision_metadata import DecisionMetadata
from domain.value_objects import StrategyDecision


# ─── DecisionMetadata VO ──────────────────────────────────────────────────────


class TestDecisionMetadataConstruction:
    def test_empty_defaults(self):
        m = DecisionMetadata()
        assert m.regime is None
        assert m.conviction is None
        assert m.window_ts is None
        assert m.dedup_key is None
        assert m.extras == {}

    def test_all_fields_set(self):
        m = DecisionMetadata(
            regime="CALM",
            conviction="HIGH",
            window_ts=1776446700,
            dedup_key="abc-123",
            extras={"chainlink_delta": 0.005},
        )
        assert m.regime == "CALM"
        assert m.conviction == "HIGH"
        assert m.window_ts == 1776446700
        assert m.extras == {"chainlink_delta": 0.005}

    def test_frozen_prevents_mutation(self):
        m = DecisionMetadata(regime="CALM")
        with pytest.raises((AttributeError, Exception)):
            m.regime = "CASCADE"  # type: ignore

    def test_empty_classmethod_returns_default(self):
        assert DecisionMetadata.empty() == DecisionMetadata()


class TestDecisionMetadataInvariants:
    def test_negative_window_ts_rejected(self):
        with pytest.raises(ValueError, match="window_ts"):
            DecisionMetadata(window_ts=-1)

    def test_window_ts_zero_allowed(self):
        """Regression: window_ts=0 is a valid timestamp, NOT a falsy-empty.
        The pre-VO code collapsed it via `or` short-circuit — VO must not."""
        m = DecisionMetadata(window_ts=0)
        assert m.window_ts == 0

    def test_window_ts_none_allowed(self):
        m = DecisionMetadata(window_ts=None)
        assert m.window_ts is None

    def test_extras_must_be_mapping(self):
        with pytest.raises(TypeError, match="extras"):
            DecisionMetadata(extras=[1, 2, 3])  # type: ignore


# ─── from_dict parser (legacy rows) ───────────────────────────────────────────


class TestDecisionMetadataFromDict:
    def test_none_input_returns_empty(self):
        assert DecisionMetadata.from_dict(None) == DecisionMetadata()

    def test_empty_dict_returns_empty(self):
        assert DecisionMetadata.from_dict({}) == DecisionMetadata()

    def test_canonical_keys_parsed(self):
        m = DecisionMetadata.from_dict(
            {
                "regime": "CALM",
                "conviction": "HIGH",
                "window_ts": 1776446700,
                "dedup_key": "abc",
            }
        )
        assert m.regime == "CALM"
        assert m.conviction == "HIGH"
        assert m.window_ts == 1776446700
        assert m.dedup_key == "abc"
        assert m.extras == {}

    def test_legacy_v4_regime_parsed(self):
        """Historical rows from configs/v4_fusion.py v4.5.0 used v4_ prefix."""
        m = DecisionMetadata.from_dict({"v4_regime": "CALM", "v4_conviction": "HIGH"})
        assert m.regime == "CALM"
        assert m.conviction == "HIGH"
        # Legacy keys must NOT also land in extras — reserved
        assert "v4_regime" not in m.extras
        assert "v4_conviction" not in m.extras

    def test_canonical_key_wins_over_legacy(self):
        """If a row has both ``regime`` AND ``v4_regime`` (shouldn't happen
        in practice but belts-and-braces) the canonical key wins."""
        m = DecisionMetadata.from_dict({"regime": "NORMAL", "v4_regime": "CALM"})
        assert m.regime == "NORMAL"

    def test_unknown_keys_land_in_extras(self):
        m = DecisionMetadata.from_dict(
            {
                "regime": "CALM",
                "chainlink_delta": 0.005,
                "tiingo_delta": 0.004,
                "health_badge": "DEGRADED",
            }
        )
        assert m.regime == "CALM"
        assert m.extras == {
            "chainlink_delta": 0.005,
            "tiingo_delta": 0.004,
            "health_badge": "DEGRADED",
        }

    def test_window_ts_zero_preserved_from_dict(self):
        m = DecisionMetadata.from_dict({"window_ts": 0})
        assert m.window_ts == 0

    def test_window_ts_missing_is_none(self):
        m = DecisionMetadata.from_dict({"regime": "CALM"})
        assert m.window_ts is None


# ─── to_dict serialisation ────────────────────────────────────────────────────


class TestDecisionMetadataToDict:
    def test_empty_round_trip(self):
        assert DecisionMetadata.empty().to_dict() == {}

    def test_none_fields_omitted(self):
        """to_dict must NOT serialise None fields — keeps JSONB terse."""
        m = DecisionMetadata(regime="CALM")
        out = m.to_dict()
        assert out == {"regime": "CALM"}
        assert "conviction" not in out
        assert "window_ts" not in out

    def test_extras_flatten_to_top_level(self):
        """Consumers reading metadata['clob_delta'] today must keep working
        — extras merge into the top-level dict, not nested."""
        m = DecisionMetadata(
            regime="CALM", extras={"clob_delta": 0.02, "health_badge": "OK"}
        )
        out = m.to_dict()
        assert out == {"regime": "CALM", "clob_delta": 0.02, "health_badge": "OK"}

    def test_window_ts_zero_serialised(self):
        """window_ts=0 must land in output, not get dropped as falsy."""
        m = DecisionMetadata(window_ts=0)
        assert m.to_dict() == {"window_ts": 0}

    def test_full_round_trip(self):
        original = {
            "regime": "CALM",
            "conviction": "HIGH",
            "window_ts": 1776446700,
            "dedup_key": "abc",
            "chainlink_delta": 0.005,
        }
        m = DecisionMetadata.from_dict(original)
        assert m.to_dict() == original

    def test_legacy_keys_normalised_on_round_trip(self):
        """Reading v4_regime and re-serialising writes the canonical form —
        this is how the migration drops the prefix for free."""
        m = DecisionMetadata.from_dict({"v4_regime": "CALM", "v4_conviction": "HIGH"})
        out = m.to_dict()
        assert out == {"regime": "CALM", "conviction": "HIGH"}
        assert "v4_regime" not in out
        assert "v4_conviction" not in out


class TestDecisionMetadataWithExtras:
    def test_merges_into_copy(self):
        m1 = DecisionMetadata(regime="CALM", extras={"a": 1})
        m2 = m1.with_extras(b=2)
        assert m2.extras == {"a": 1, "b": 2}
        # Original untouched
        assert m1.extras == {"a": 1}

    def test_overrides_on_collision(self):
        m1 = DecisionMetadata(extras={"k": "old"})
        m2 = m1.with_extras(k="new")
        assert m2.extras == {"k": "new"}

    def test_preserves_shared_fields(self):
        m1 = DecisionMetadata(regime="CALM", conviction="HIGH", window_ts=100)
        m2 = m1.with_extras(x=1)
        assert m2.regime == "CALM"
        assert m2.conviction == "HIGH"
        assert m2.window_ts == 100


# ─── StrategyDecision factory methods ─────────────────────────────────────────


class TestStrategyDecisionTradeFactory:
    def test_trade_with_vo_metadata(self):
        meta = DecisionMetadata(regime="CALM", conviction="HIGH", window_ts=100)
        d = StrategyDecision.trade(
            direction="UP",
            strategy_id="v4_fusion",
            strategy_version="4.5.0",
            entry_reason="polymarket_v2_calm",
            metadata=meta,
            confidence="HIGH",
            entry_cap=0.65,
        )
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v4_fusion"
        # Serialised to canonical dict
        assert d.metadata == {"regime": "CALM", "conviction": "HIGH", "window_ts": 100}

    def test_trade_with_dict_metadata_legacy(self):
        """Legacy builders that still pass a raw dict must keep working
        during the migration window."""
        d = StrategyDecision.trade(
            direction="DOWN",
            strategy_id="v4_fusion",
            strategy_version="4.5.0",
            entry_reason="test",
            metadata={"v4_regime": "CASCADE"},
        )
        # Dict passes through unchanged — migration preserves shape
        assert d.metadata == {"v4_regime": "CASCADE"}

    def test_trade_with_none_metadata(self):
        d = StrategyDecision.trade(
            direction="UP",
            strategy_id="s",
            strategy_version="1.0",
            entry_reason="t",
        )
        assert d.metadata == {}

    def test_trade_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            StrategyDecision.trade(
                direction="SIDEWAYS",  # type: ignore
                strategy_id="s",
                strategy_version="1.0",
                entry_reason="t",
            )

    def test_trade_rejects_none_direction(self):
        with pytest.raises(ValueError, match="direction"):
            StrategyDecision.trade(
                direction=None,  # type: ignore
                strategy_id="s",
                strategy_version="1.0",
                entry_reason="t",
            )


class TestStrategyDecisionSkipFactory:
    def test_skip_with_vo_metadata(self):
        meta = DecisionMetadata(regime="CALM", extras={"reason_detail": "below_floor"})
        d = StrategyDecision.skip(
            reason="regime:calm_skip",
            strategy_id="v4_fusion",
            strategy_version="4.5.0",
            metadata=meta,
        )
        assert d.action == "SKIP"
        assert d.direction is None
        assert d.skip_reason == "regime:calm_skip"
        assert d.metadata == {"regime": "CALM", "reason_detail": "below_floor"}

    def test_skip_no_metadata_defaults_empty(self):
        d = StrategyDecision.skip(
            reason="no_signal",
            strategy_id="s",
            strategy_version="1.0",
        )
        assert d.metadata == {}

    def test_skip_preserves_pre_decision_fields(self):
        """Post-hook-gate skip: strategy had computed direction + confidence
        but a YAML post-filter vetoed. The factory must preserve those
        fields so the Signal Explorer can render "would have traded UP
        but was filtered by gate X"."""
        d = StrategyDecision.skip(
            reason="post_hook_gate tiingo_agreement: tiingo_disagrees",
            strategy_id="v4_fusion",
            strategy_version="4.5.0",
            direction="UP",
            confidence="HIGH",
            confidence_score=0.72,
            entry_cap=0.65,
            collateral_pct=0.10,
            entry_reason="polymarket_v2_calm",
        )
        assert d.action == "SKIP"
        assert d.direction == "UP"
        assert d.confidence == "HIGH"
        assert d.confidence_score == 0.72
        assert d.entry_cap == 0.65
        assert d.collateral_pct == 0.10
        assert d.entry_reason == "polymarket_v2_calm"
        assert d.skip_reason.startswith("post_hook_gate")


class TestStrategyDecisionErrorFactory:
    def test_error_preserves_reason(self):
        d = StrategyDecision.error(
            reason="surface_missing_v4",
            strategy_id="v4_fusion",
            strategy_version="4.5.0",
        )
        assert d.action == "ERROR"
        assert d.skip_reason == "surface_missing_v4"
        assert d.direction is None
        assert d.metadata == {}


class TestMetadataCoercion:
    """The _coerce_metadata shim is the migration linchpin — it must
    accept exactly three shapes (None, dict, DecisionMetadata) and
    reject everything else loudly."""

    def test_coerce_rejects_other_types(self):
        with pytest.raises(TypeError, match="metadata"):
            StrategyDecision.trade(
                direction="UP",
                strategy_id="s",
                strategy_version="1.0",
                entry_reason="t",
                metadata=["not", "a", "dict"],  # type: ignore
            )
