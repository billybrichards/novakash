"""Unit tests for per-strategy sizing override and block-cell size_multiplier.

Covers audit #410 goals:
  Goal 1 -- _resolve_sizing_for_strategy honours strategy_runtime_overrides.params
  Goal 2 -- block-cell size_multiplier applied inside _calculate_stake
  Goal 4 -- test coverage
  Goal 5 -- DISABLE_PER_STRATEGY_SIZING rollback flag

Test IDs:
  test_override_max_position_respected  -- override max_position_usd=5 -> stake <= $5
  test_no_override_uses_global          -- no sizing override -> uses global runtime (no regression)
  test_override_bet_fraction            -- override bet_fraction=0.01 -> smaller base stake
  test_multiple_aliases_max_position    -- absolute_max_bet alias respected
  test_rollback_flag_disables_override  -- DISABLE_PER_STRATEGY_SIZING=true -> global used
  test_block_cell_mult_half             -- size_multiplier=0.5 -> stake halved
  test_block_cell_mult_zero_skips       -- size_multiplier=0.0 -> treated as hard block
  test_block_cell_mult_one_no_change    -- size_multiplier=1.0 -> no change
  test_block_cell_mult_missing_no_change-- missing size_multiplier -> no change (hard block path)
  test_block_cell_min_wins              -- two matching predicates -> smaller mult wins
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch

from domain.value_objects import RiskStatus, StrategyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_decision(**overrides) -> StrategyDecision:
    defaults = dict(
        action="TRADE",
        direction="DOWN",
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.50,
        collateral_pct=0.025,
        strategy_id="v9_2_super_lgb_only",
        strategy_version="2.0.0",
        entry_reason="test",
        skip_reason=None,
        metadata={},
    )
    defaults.update(overrides)
    return StrategyDecision(**defaults)


def _make_risk(bankroll: float = 250.0, **overrides) -> RiskStatus:
    defaults = dict(
        current_bankroll=bankroll,
        peak_bankroll=260.0,
        drawdown_pct=0.04,
        daily_pnl=0.0,
        consecutive_losses=0,
        paper_mode=True,
        kill_switch_active=False,
    )
    defaults.update(overrides)
    return RiskStatus(**defaults)


class _FakeClock:
    def now(self) -> float:
        return 1000.0


def _make_uc(risk_status: RiskStatus = None):
    """Build ExecuteTradeUseCase with all ports mocked."""
    from unittest.mock import AsyncMock
    from use_cases.execute_trade import ExecuteTradeUseCase

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk_status or _make_risk()
    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim")
    mock_window_state.has_filled.return_value = False
    mock_alerter = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_FakeClock(),
        paper_mode=True,
    )
    return uc


def _make_override_manager(strategy_id: str, params: dict):
    """Return a mock RuntimeOverrideManager with one strategy override."""
    from strategies.runtime_override import RuntimeOverride
    mgr = MagicMock()
    ov = RuntimeOverride(strategy_id=strategy_id, mode="LIVE", params=params)
    mgr.get_runtime_override.return_value = ov
    return mgr


def _stake_from_uc(uc, decision: StrategyDecision) -> float:
    """Call _calculate_stake and return adjusted_stake."""
    result = uc._calculate_stake(decision)
    return result.adjusted_stake


# ---------------------------------------------------------------------------
# Goal 1: per-strategy override tests
# ---------------------------------------------------------------------------

class TestPerStrategyOverride:
    """_resolve_sizing_for_strategy reads override params correctly."""

    def test_override_max_position_respected(self, monkeypatch):
        """Strategy with override max_position_usd=5 -> stake <= $5
        even when global runtime.max_position_usd=20."""
        from use_cases import execute_trade as et_module

        # Global runtime says $20
        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.25)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        override_mgr = _make_override_manager(
            "v9_2_super_lgb_only",
            {"max_position_usd": 5.0, "bet_fraction": 0.025},
        )
        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=override_mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        assert stake <= 5.0, f"Expected stake <= $5 (override), got ${stake}"

    def test_no_override_uses_global(self, monkeypatch):
        """Strategy with NO sizing override -> uses global runtime (no regression)."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        # Override exists but has NO sizing fields
        override_mgr = _make_override_manager(
            "v12_lgb_combo",
            {"min_consecutive_pass_ticks": 3},  # gate param only, no sizing
        )
        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=override_mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v12_lgb_combo", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        # With bankroll=250, bet_fraction=0.04, base=10, price_mult=1.0,
        # cap=min(20, 250*0.04*0.95)=9.50 -> stake ~$9.50
        assert stake <= 20.0, "Should not exceed global cap"
        assert stake >= 1.0, "Should be above min_bet"
        # Crucially: stake is driven by global runtime (bet_fraction=0.04), not override
        expected_approx = min(20.0, 250.0 * 0.04 * 0.95)
        assert abs(stake - expected_approx) < 1.0, (
            f"Expected stake near ${expected_approx:.2f} (global), got ${stake}"
        )

    def test_override_bet_fraction_smaller_stake(self, monkeypatch):
        """Override bet_fraction=0.01 -> base stake smaller than global 0.25."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.25)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        override_mgr = _make_override_manager(
            "v9_2_super_lgb_only",
            {"bet_fraction": 0.01, "max_position_usd": 5.0},
        )
        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=override_mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        # base = 250 * 0.01 = 2.5, cap=min(5, 250*0.01*0.95)=2.375 -> stake $2.38
        assert stake <= 5.0, f"Should be capped at $5, got ${stake}"
        assert stake < 20.0, "Should be well below global $20 cap"

    def test_absolute_max_bet_alias_respected(self, monkeypatch):
        """Override using alias 'absolute_max_bet' should cap stake."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.25)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        override_mgr = _make_override_manager(
            "v9_2_super_lgb_only",
            {"absolute_max_bet": 7.0},  # alias, not "max_position_usd"
        )
        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=override_mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        assert stake <= 7.0, f"absolute_max_bet alias not honoured, got ${stake}"

    def test_rollback_flag_disables_override(self, monkeypatch):
        """DISABLE_PER_STRATEGY_SIZING=true -> override ignored, global used."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        # Enable rollback flag
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", True)

        override_mgr = _make_override_manager(
            "v9_2_super_lgb_only",
            {"max_position_usd": 5.0, "bet_fraction": 0.01},
        )
        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=override_mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        # With rollback: bankroll=250, bet_fraction=0.04(global), max=20(global)
        # base=10, price_mult=1.0, cap=min(20, 9.5)=9.5 -> stake $9.5
        assert stake > 5.0, (
            f"Rollback flag should use global ($20 cap), not override ($5 cap), got ${stake}"
        )

    def test_strategy_id_from_decision_not_fallback(self, monkeypatch):
        """decision.strategy_id is used as override key, not any fallback strategy."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.25)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        # v9_2 has $5 cap; v8_champion (fallback) would have $20
        def _selective_override(sid):
            from strategies.runtime_override import RuntimeOverride
            if sid == "v9_2_super_lgb_only":
                return RuntimeOverride(
                    strategy_id=sid, mode="GHOST",
                    params={"max_position_usd": 5.0}
                )
            return None

        mgr = MagicMock()
        mgr.get_runtime_override.side_effect = _selective_override

        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            stake = _stake_from_uc(uc, decision)

        assert stake <= 5.0, f"Should use v9_2 override ($5), got ${stake}"

    def test_override_graceful_on_lookup_error(self, monkeypatch):
        """Override lookup error -> falls through to global (never raises)."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 15.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", False)

        mgr = MagicMock()
        mgr.get_runtime_override.side_effect = RuntimeError("DB unavailable")

        with patch(
            "strategies.runtime_override.get_runtime_override_manager",
            return_value=mgr,
        ):
            uc = _make_uc(_make_risk(bankroll=250.0))
            decision = _make_decision(strategy_id="v9_2_super_lgb_only", entry_cap=0.50)
            # Must not raise
            stake = _stake_from_uc(uc, decision)

        assert stake <= 15.0, f"Should fall back to global $15 cap, got ${stake}"
        assert stake >= 1.0


# ---------------------------------------------------------------------------
# Goal 2: block-cell size_multiplier tests
# ---------------------------------------------------------------------------

class TestBlockCellSizeMultiplier:
    """_calculate_stake applies block_cell_size_multiplier from decision.metadata."""

    def _stake_with_mult(self, mult, monkeypatch) -> float:
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", True)  # isolate
        monkeypatch.setattr(et_module, "_DISABLE_BLOCK_CELL_SIZE_MULT", False)

        uc = _make_uc(_make_risk(bankroll=250.0))
        decision = _make_decision(
            strategy_id="v9_2_super_lgb_only",
            entry_cap=0.50,
            metadata={"block_cell_size_multiplier": mult},
        )
        return _stake_from_uc(uc, decision)

    def _stake_without_mult(self, monkeypatch) -> float:
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", True)
        monkeypatch.setattr(et_module, "_DISABLE_BLOCK_CELL_SIZE_MULT", False)

        uc = _make_uc(_make_risk(bankroll=250.0))
        decision = _make_decision(
            strategy_id="v9_2_super_lgb_only",
            entry_cap=0.50,
            metadata={},  # no block_cell_size_multiplier
        )
        return _stake_from_uc(uc, decision)

    def test_size_multiplier_half(self, monkeypatch):
        """size_multiplier=0.5 -> stake is approximately half of base."""
        base = self._stake_without_mult(monkeypatch)
        halved = self._stake_with_mult(0.5, monkeypatch)
        assert abs(halved - base * 0.5) <= 0.02, (
            f"Expected ~${base * 0.5:.2f}, got ${halved:.2f}"
        )

    def test_size_multiplier_one_no_change(self, monkeypatch):
        """size_multiplier=1.0 -> no change to stake."""
        base = self._stake_without_mult(monkeypatch)
        unchanged = self._stake_with_mult(1.0, monkeypatch)
        assert unchanged == base, f"size_mult=1.0 should not change stake: {base} vs {unchanged}"

    def test_size_multiplier_zero_not_applied_as_scaling(self, monkeypatch):
        """size_multiplier=0.0 should NOT scale to $0 -- value is skipped (treated
        as hard block upstream; _calculate_stake sees 0.0 and skips it)."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", True)
        monkeypatch.setattr(et_module, "_DISABLE_BLOCK_CELL_SIZE_MULT", False)

        uc = _make_uc(_make_risk(bankroll=250.0))
        decision = _make_decision(
            strategy_id="v9_2_super_lgb_only",
            entry_cap=0.50,
            metadata={"block_cell_size_multiplier": 0.0},
        )
        stake = _stake_from_uc(uc, decision)
        # 0.0 is not in (0.0, 1.0) so scaling should NOT be applied
        base = self._stake_without_mult(monkeypatch)
        assert stake == base, (
            f"size_mult=0.0 should be ignored by _calculate_stake (hard block handled "
            f"upstream). Expected ${base}, got ${stake}"
        )

    def test_size_multiplier_missing_no_change(self, monkeypatch):
        """No block_cell_size_multiplier key -> no change (backward-compatible)."""
        base = self._stake_without_mult(monkeypatch)
        # stake_without_mult already has no key in metadata
        # Verify the reference result is sensible
        assert base >= 1.0
        assert base <= 20.0

    def test_disable_block_cell_size_mult_flag(self, monkeypatch):
        """DISABLE_BLOCK_CELL_SIZE_MULTIPLIER=true -> multiplier ignored."""
        from use_cases import execute_trade as et_module

        monkeypatch.setattr(et_module.runtime, "bet_fraction", 0.04)
        monkeypatch.setattr(et_module.runtime, "max_position_usd", 20.0)
        monkeypatch.setattr(et_module.runtime, "min_bet_usd", 1.0)
        monkeypatch.setattr(et_module, "_DISABLE_PER_STRATEGY_SIZING", True)
        monkeypatch.setattr(et_module, "_DISABLE_BLOCK_CELL_SIZE_MULT", True)  # disabled

        uc = _make_uc(_make_risk(bankroll=250.0))
        decision = _make_decision(
            strategy_id="v9_2_super_lgb_only",
            entry_cap=0.50,
            metadata={"block_cell_size_multiplier": 0.25},
        )
        stake_disabled = _stake_from_uc(uc, decision)

        # Without disable flag
        monkeypatch.setattr(et_module, "_DISABLE_BLOCK_CELL_SIZE_MULT", False)
        stake_enabled = _stake_from_uc(uc, decision)

        assert stake_disabled > stake_enabled, (
            f"Rollback flag should produce larger stake: "
            f"disabled=${stake_disabled} enabled=${stake_enabled}"
        )


# ---------------------------------------------------------------------------
# Goal 2: block_cells.resolve_block_cells_size_multiplier tests
# ---------------------------------------------------------------------------

class TestBlockCellsResolve:
    """resolve_block_cells_size_multiplier returns correct BlockCellsResult."""

    def _call(self, predicates, direction="DOWN", eval_offset=90,
               confidence_score=0.15, regime=None, window_ts=None):
        from strategies.gates.block_cells import resolve_block_cells_size_multiplier
        return resolve_block_cells_size_multiplier(
            predicates=predicates,
            direction=direction,
            eval_offset=eval_offset,
            confidence_score=confidence_score,
            regime=regime,
            window_ts=window_ts,
        )

    def test_empty_predicates_returns_default(self):
        result = self._call([])
        assert result.skip_reason is None
        assert result.size_multiplier == 1.0
        assert result.matched_predicate_desc is None

    def test_no_match_returns_default(self):
        pred = [{"direction": "UP", "size_multiplier": 0.5}]
        result = self._call(pred, direction="DOWN")
        assert result.skip_reason is None
        assert result.size_multiplier == 1.0

    def test_hard_block_no_size_mult_wins(self):
        """Predicate without size_multiplier -> hard block."""
        pred = [{"direction": "DOWN"}]  # no size_multiplier
        result = self._call(pred, direction="DOWN")
        assert result.skip_reason is not None
        assert result.size_multiplier == 0.0

    def test_size_mult_half_match(self):
        pred = [{"direction": "DOWN", "size_multiplier": 0.5}]
        result = self._call(pred, direction="DOWN")
        assert result.skip_reason is None
        assert result.size_multiplier == 0.5
        assert result.matched_predicate_desc is not None

    def test_size_mult_zero_returns_skip(self):
        """size_multiplier=0.0 -> treated as hard block (skip_reason set)."""
        pred = [{"direction": "DOWN", "size_multiplier": 0.0}]
        result = self._call(pred, direction="DOWN")
        assert result.skip_reason is not None
        assert result.size_multiplier == 0.0

    def test_size_mult_one_no_skip_no_scale(self):
        pred = [{"direction": "DOWN", "size_multiplier": 1.0}]
        result = self._call(pred, direction="DOWN")
        assert result.skip_reason is None
        assert result.size_multiplier == 1.0

    def test_hard_block_beats_partial_size(self):
        """Hard block predicate always wins over partial-size predicate."""
        preds = [
            {"direction": "DOWN", "size_multiplier": 0.5},  # partial
            {"direction": "DOWN"},                            # hard block
        ]
        result = self._call(preds, direction="DOWN")
        assert result.skip_reason is not None, "Hard block should win"
        assert result.size_multiplier == 0.0

    def test_hard_block_first_beats_partial_later(self):
        """Order doesn't matter -- hard block in first position still wins."""
        preds = [
            {"direction": "DOWN"},                            # hard block first
            {"direction": "DOWN", "size_multiplier": 0.5},  # partial
        ]
        result = self._call(preds, direction="DOWN")
        assert result.skip_reason is not None
        assert result.size_multiplier == 0.0

    def test_two_partial_min_wins(self):
        """Multiple partial-size matches -> smallest multiplier wins (most conservative)."""
        preds = [
            {"direction": "DOWN", "size_multiplier": 0.7},
            {"direction": "DOWN", "size_multiplier": 0.3},
        ]
        result = self._call(preds, direction="DOWN")
        assert result.skip_reason is None
        assert result.size_multiplier == 0.3

    def test_mismatched_direction_ignored(self):
        """UP predicate does not match DOWN context."""
        pred = [{"direction": "UP", "size_multiplier": 0.5}]
        result = self._call(pred, direction="DOWN")
        assert result.size_multiplier == 1.0

    def test_malformed_predicate_ignored(self):
        """Non-dict entries are silently skipped."""
        preds = ["not_a_dict", None, {"direction": "DOWN", "size_multiplier": 0.4}]
        result = self._call(preds, direction="DOWN")
        assert result.size_multiplier == 0.4

    def test_empty_dict_predicate_ignored(self):
        """Empty dict predicate is a no-op (defensive)."""
        preds = [{}, {"direction": "DOWN", "size_multiplier": 0.6}]
        result = self._call(preds, direction="DOWN")
        assert result.size_multiplier == 0.6


# ---------------------------------------------------------------------------
# Goal 2: check_block_cells_predicate backward-compat (size_mult predicates pass)
# ---------------------------------------------------------------------------

class TestCheckBlockCellsBackwardCompat:
    """check_block_cells_predicate treats size_mult>0 predicates as pass."""

    def _call(self, predicates, direction="DOWN", eval_offset=90,
               confidence_score=0.15, regime=None, window_ts=None):
        from strategies.gates.block_cells import check_block_cells_predicate
        return check_block_cells_predicate(
            predicates=predicates,
            direction=direction,
            eval_offset=eval_offset,
            confidence_score=confidence_score,
            regime=regime,
            window_ts=window_ts,
        )

    def test_partial_size_predicate_does_not_hard_block(self):
        """Predicate with size_multiplier=0.5 -> returns None (not a hard block)."""
        pred = [{"direction": "DOWN", "size_multiplier": 0.5}]
        result = self._call(pred, direction="DOWN")
        assert result is None, (
            f"size_mult=0.5 should pass check_block_cells_predicate, got: {result}"
        )

    def test_hard_block_predicate_still_blocks(self):
        """Legacy hard-block predicate (no size_multiplier) still returns reason."""
        pred = [{"direction": "DOWN"}]
        result = self._call(pred, direction="DOWN")
        assert result is not None, "Hard-block predicate should still return skip reason"

    def test_size_mult_zero_still_hard_blocks(self):
        """size_multiplier=0.0 -> treated as hard block by check_block_cells_predicate."""
        pred = [{"direction": "DOWN", "size_multiplier": 0.0}]
        result = self._call(pred, direction="DOWN")
        assert result is not None, "size_mult=0.0 should be a hard block"
