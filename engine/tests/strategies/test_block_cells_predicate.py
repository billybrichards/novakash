"""Tests for the per-cell block predicate helper (audit #382, 2026-05-07).

Unit-tests for ``strategies.gates.block_cells.check_block_cells_predicate``
plus an integration test verifying that v9_ensemble.evaluate_v9_ensemble
skips when a ``block_cells`` runtime override matches.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

from strategies.gates.block_cells import check_block_cells_predicate


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _call(
    predicates: list[dict[str, Any]],
    *,
    direction: str = "DOWN",
    eval_offset: Optional[int] = 80,   # → T-61-90
    confidence_score: float = 0.30,     # abs(0.80 - 0.5)
    regime: Optional[str] = "chop",
    window_ts: Optional[int] = None,    # → session=unknown
) -> Optional[str]:
    return check_block_cells_predicate(
        predicates=predicates,
        direction=direction,
        eval_offset=eval_offset,
        confidence_score=confidence_score,
        regime=regime,
        window_ts=window_ts,
    )


# T-61-90 at hour 10 UTC (eu_am) — baseline surface params:
_HOUR_10_UTC_TS = 1_778_148_000  # 2026-05-07 10:00:00 UTC


# ─────────────────────────────────────────────────────────────────────────────
# Core correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyPredicates:
    def test_empty_list_returns_none(self):
        assert _call([]) is None

    def test_empty_dict_predicate_returns_none(self):
        """A predicate with no fields is a no-op (defensive guard)."""
        assert _call([{}]) is None

    def test_none_value_in_list_does_not_crash(self):
        """Malformed entry in the list — should skip silently, not raise."""
        assert _call([None]) is None  # type: ignore[list-item]

    def test_non_dict_in_list_does_not_crash(self):
        assert _call(["bad"]) is None  # type: ignore[list-item]


class TestDirectionMatch:
    def test_direction_match_blocks(self):
        result = _call([{"direction": "DOWN"}], direction="DOWN")
        assert result is not None
        assert "block_cells" in result

    def test_direction_mismatch_passes(self):
        result = _call([{"direction": "UP"}], direction="DOWN")
        assert result is None

    def test_up_direction_matches(self):
        result = _call([{"direction": "UP"}], direction="UP")
        assert result is not None


class TestTBandMatch:
    def test_t_band_match_blocks(self):
        # eval_offset=80 → T-61-90
        result = _call([{"t_band": "T-61-90"}], eval_offset=80)
        assert result is not None

    def test_t_band_mismatch_passes(self):
        result = _call([{"t_band": "T-91-120"}], eval_offset=80)
        assert result is None

    def test_t_band_exact_boundary_lower(self):
        # eval_offset=61 → T-61-90
        assert _call([{"t_band": "T-61-90"}], eval_offset=61) is not None

    def test_t_band_exact_boundary_upper(self):
        # eval_offset=90 → T-61-90
        assert _call([{"t_band": "T-61-90"}], eval_offset=90) is not None

    def test_t_band_just_outside(self):
        # eval_offset=91 → T-91-120
        assert _call([{"t_band": "T-61-90"}], eval_offset=91) is None


class TestConfidenceMatch:
    def test_conf_min_inclusive_at_boundary(self):
        """conf_min is INCLUSIVE: score == conf_min should match."""
        result = _call([{"conf_min": 0.30}], confidence_score=0.30)
        assert result is not None

    def test_conf_min_blocks_when_score_above(self):
        result = _call([{"conf_min": 0.25}], confidence_score=0.30)
        assert result is not None

    def test_conf_min_passes_when_score_below(self):
        result = _call([{"conf_min": 0.35}], confidence_score=0.30)
        assert result is None

    def test_conf_max_exclusive_at_boundary(self):
        """conf_max is EXCLUSIVE: score == conf_max should NOT match."""
        result = _call([{"conf_max": 0.30}], confidence_score=0.30)
        assert result is None

    def test_conf_max_blocks_when_score_below(self):
        result = _call([{"conf_max": 0.35}], confidence_score=0.30)
        assert result is not None

    def test_conf_max_passes_when_score_above(self):
        result = _call([{"conf_max": 0.25}], confidence_score=0.30)
        assert result is None

    def test_conf_min_only_open_upper(self):
        """Predicate with only conf_min — any score >= threshold matches."""
        result = _call([{"conf_min": 0.45}], confidence_score=0.50)
        assert result is not None

    def test_conf_max_only_open_lower(self):
        """Predicate with only conf_max — any score < threshold matches."""
        result = _call([{"conf_max": 0.35}], confidence_score=0.10)
        assert result is not None

    def test_conf_band_both_bounds(self):
        """[0.25, 0.35) — inside matches, outside passes."""
        pred = [{"conf_min": 0.25, "conf_max": 0.35}]
        assert _call(pred, confidence_score=0.25) is not None  # inclusive lower
        assert _call(pred, confidence_score=0.30) is not None  # inside
        assert _call(pred, confidence_score=0.35) is None       # exclusive upper
        assert _call(pred, confidence_score=0.24) is None       # below lower


class TestRegimeMatch:
    def test_regime_match_blocks(self):
        result = _call([{"regime": "CASCADE"}], regime="CASCADE")
        assert result is not None

    def test_regime_mismatch_passes(self):
        result = _call([{"regime": "CASCADE"}], regime="chop")
        assert result is None

    def test_regime_none_when_predicate_set_passes(self):
        result = _call([{"regime": "CASCADE"}], regime=None)
        assert result is None


class TestSessionMatch:
    def test_session_match_blocks(self):
        # hour 10 UTC → eu_am, window_ts set to hour-10 epoch
        result = _call(
            [{"session": "eu_am"}],
            window_ts=_HOUR_10_UTC_TS,
        )
        assert result is not None

    def test_session_mismatch_passes(self):
        result = _call(
            [{"session": "us_pm"}],
            window_ts=_HOUR_10_UTC_TS,
        )
        assert result is None

    def test_session_unknown_when_no_window_ts(self):
        """No window_ts → session='unknown'; predicate for eu_am does not match."""
        result = _call([{"session": "eu_am"}], window_ts=None)
        assert result is None


class TestHourUtcMatch:
    """Hour-precision blocks via the `hour_utc` field — preferred over
    session for single-hour weak cells where session_label's 4-hour
    bucket would over-block adjacent hours."""

    def test_hour_utc_match_blocks(self):
        # hour 10 UTC, predicate matches hour 10
        result = _call([{"hour_utc": 10}], window_ts=_HOUR_10_UTC_TS)
        assert result is not None
        assert "hour_utc=10" in result

    def test_hour_utc_mismatch_passes(self):
        # hour 10 UTC, predicate matches hour 19
        result = _call([{"hour_utc": 19}], window_ts=_HOUR_10_UTC_TS)
        assert result is None

    def test_hour_utc_with_no_window_ts_passes(self):
        """No window_ts → hour_utc=None; predicate cannot match."""
        result = _call([{"hour_utc": 10}], window_ts=None)
        assert result is None

    def test_hour_utc_invalid_value_passes(self):
        """Non-numeric hour_utc in predicate → silent no-match (defensive)."""
        result = _call([{"hour_utc": "ten"}], window_ts=_HOUR_10_UTC_TS)
        assert result is None

    def test_hour_utc_with_direction_compound(self):
        """Direction + hour_utc — both must match (mirrors v9.2 use case)."""
        # hour 19 UTC weak for UP only (per v9.2 alpha-mining)
        hour_19_ts = 1_778_180_400  # 2026-05-07 19:00:00 UTC
        pred = [{"direction": "UP", "hour_utc": 19}]
        # UP + h19 → block
        assert _call(pred, direction="UP", window_ts=hour_19_ts) is not None
        # DOWN + h19 → pass (DOWN at h19 is fine, 94.5% WR)
        assert _call(pred, direction="DOWN", window_ts=hour_19_ts) is None
        # UP + h20 → pass (h20 is gold hour, 99.2% WR)
        hour_20_ts = 1_778_184_000
        assert _call(pred, direction="UP", window_ts=hour_20_ts) is None


class TestMultiFieldPredicate:
    def test_all_fields_must_match(self):
        """A predicate with direction + t_band — only matches when both fit."""
        pred = [{"direction": "DOWN", "t_band": "T-61-90"}]
        # Both match → block
        assert _call(pred, direction="DOWN", eval_offset=80) is not None
        # direction wrong → pass
        assert _call(pred, direction="UP", eval_offset=80) is None
        # t_band wrong → pass
        assert _call(pred, direction="DOWN", eval_offset=100) is None

    def test_full_five_field_predicate(self):
        """Match only when all five optional fields agree."""
        pred = [
            {
                "direction": "DOWN",
                "t_band": "T-61-90",
                "conf_min": 0.45,
                "regime": "CASCADE",
                "session": "eu_am",
            }
        ]
        # All match
        assert _call(
            pred,
            direction="DOWN",
            eval_offset=80,
            confidence_score=0.48,
            regime="CASCADE",
            window_ts=_HOUR_10_UTC_TS,
        ) is not None
        # One field off (regime)
        assert _call(
            pred,
            direction="DOWN",
            eval_offset=80,
            confidence_score=0.48,
            regime="chop",
            window_ts=_HOUR_10_UTC_TS,
        ) is None


class TestMultiplePredicates:
    def test_first_match_wins(self):
        """Multiple predicates: returns on the first match."""
        preds = [
            {"direction": "UP", "t_band": "T-61-90"},
            {"direction": "DOWN", "t_band": "T-61-90"},
        ]
        result = _call(preds, direction="DOWN", eval_offset=80)
        assert result is not None

    def test_no_match_in_multiple_passes(self):
        preds = [
            {"direction": "UP", "t_band": "T-61-90"},
            {"direction": "DOWN", "t_band": "T-91-120"},
        ]
        # direction=DOWN but t_band=T-61-90 (neither pred matches)
        result = _call(preds, direction="DOWN", eval_offset=80)
        assert result is None


class TestReturnFormat:
    def test_reason_contains_block_cells_prefix(self):
        result = _call([{"direction": "DOWN"}], direction="DOWN")
        assert result is not None
        assert result.startswith("block_cells:")

    def test_reason_contains_direction(self):
        result = _call(
            [{"direction": "DOWN", "t_band": "T-61-90", "conf_min": 0.25, "conf_max": 0.35}],
            direction="DOWN",
            eval_offset=80,
            confidence_score=0.30,
        )
        assert result is not None
        assert "DOWN" in result
        assert "T-61-90" in result


# ─────────────────────────────────────────────────────────────────────────────
# Integration: v9_ensemble.evaluate_v9_ensemble honours block_cells override
# ─────────────────────────────────────────────────────────────────────────────

def _make_surface(**overrides):
    """Minimal FullDataSurface-like namespace designed to pass v9 gates up to
    the block_cells check (gate 9c). Values chosen so all gates 1-9b pass.

    Key values:
    - eval_offset=80 → T-61-90 (inside default min/max window for v9)
    - window_ts at 2026-05-07 10:00 UTC → hour=10 → eu_am session
      (not hour 0 which could be blocked by utc_hour_block)
    - probability_lgb=0.82 → direction=UP, pl_dist=0.32 (clears safety floor)
    - probability_classifier=0.78 → direction=UP (agrees with lgb)
    - chainlink + tiingo both positive → oracle direction = UP
    """
    defaults = dict(
        eval_offset=80,            # T-61-90, inside default min/max window
        window_ts=_HOUR_10_UTC_TS,  # 2026-05-07 10:00 UTC → hour=10 (eu_am)
        v4_regime="chop",
        regime="chop",
        vpin=0.50,
        probability_lgb=0.82,       # LGB: UP direction, dist=0.32
        probability_classifier=0.78,  # PC: UP direction
        delta_chainlink=0.0003,    # positive → UP
        delta_tiingo=0.0002,       # positive → UP
        delta_binance=None,
        delta_coinglass=None,
        poly_max_entry_price=0.65,  # fills fill_band gate
        clob_up_ask=0.65,
        clob_down_ask=0.35,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_v9_ensemble_skips_when_block_cells_matches():
    """Integration: evaluate_v9_ensemble must return SKIP when a predicate fires."""
    from strategies.gates.block_cells import check_block_cells_predicate
    from strategies import gate_params as _gp

    # The surface will have direction=UP (lgb > 0.5) with pl_dist ≈ 0.32 and
    # eval_offset=80 → T-61-90.  We block UP × T-61-90 × conf_min=0.25.
    predicates = [
        {"direction": "UP", "t_band": "T-61-90", "conf_min": 0.25},
    ]

    surface = _make_surface()

    with patch.object(_gp, "_ACTIVE") as mock_active:
        mock_active.get.return_value = {"block_cells": predicates}
        from strategies.configs.v9_ensemble import evaluate_v9_ensemble
        decision = evaluate_v9_ensemble(surface)

    assert decision.action == "SKIP"
    assert decision.skip_reason is not None
    assert "block_cells" in decision.skip_reason


def test_v9_ensemble_passes_when_block_cells_empty():
    """Integration: empty block_cells → gate is a no-op (no extra skip)."""
    from strategies import gate_params as _gp

    surface = _make_surface()

    # We can't easily get all 15 gates to pass in isolation because of
    # the 3-tick confirmation state machine. Instead just verify that the
    # block_cells gate itself does not cause a premature skip.
    # We do this by confirming that the skip_reason (if any) is NOT about
    # block_cells when the list is empty.
    with patch.object(_gp, "_ACTIVE") as mock_active:
        mock_active.get.return_value = {"block_cells": []}
        from strategies.configs.v9_ensemble import evaluate_v9_ensemble
        decision = evaluate_v9_ensemble(surface)

    if decision.action == "SKIP":
        assert "block_cells" not in (decision.skip_reason or "")


def test_v9_ensemble_skip_reason_contains_predicate_details():
    """Integration: the skip reason must describe which predicate fired."""
    from strategies import gate_params as _gp

    predicates = [
        {"direction": "UP", "t_band": "T-61-90", "conf_min": 0.45},  # won't match
        {"direction": "UP", "t_band": "T-61-90", "conf_min": 0.25, "conf_max": 0.35},
    ]
    surface = _make_surface()

    with patch.object(_gp, "_ACTIVE") as mock_active:
        mock_active.get.return_value = {"block_cells": predicates}
        from strategies.configs.v9_ensemble import evaluate_v9_ensemble
        decision = evaluate_v9_ensemble(surface)

    assert decision.action == "SKIP"
    reason = decision.skip_reason or ""
    assert "T-61-90" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Regression: block_cells must fire on the pc-null fallback path (fix Hub #434)
#
# Prior to the fix, when probability_classifier=None the code at line ~660
# called evaluate_v8_champion_lgb_only(surface) and returned immediately,
# making the block_cells gate at line ~1005 unreachable.  All 200 recent
# v9_1_lgb_only and v12_lgb_combo trades bypassed block_cells silently.
# ─────────────────────────────────────────────────────────────────────────────

def _make_surface_pc_null(**overrides):
    """Like _make_surface but with probability_classifier=None.

    With pc=None and fallback_to_lgb_on_pc_null=True the engine falls back to
    evaluate_v8_champion_lgb_only. The LGB signal (pl=0.82 → UP) passes the
    v8 gate stack (all v8 gates use the same surface fields), so the fallback
    returns a TRADE UP decision — which is the path the block_cells regression
    must intercept.
    """
    base = dict(
        eval_offset=80,            # T-61-90
        window_ts=_HOUR_10_UTC_TS,  # hour=10 UTC → eu_am
        v4_regime="chop",
        regime="chop",
        vpin=0.50,
        probability_lgb=0.82,       # LGB: UP, dist=0.32
        probability_classifier=None,  # ← triggers pc-null fallback
        delta_chainlink=0.0003,
        delta_tiingo=0.0002,
        delta_binance=None,
        delta_coinglass=None,
        poly_max_entry_price=0.65,
        clob_up_ask=0.65,
        clob_down_ask=0.35,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPcNullFallbackBlockCells:
    """Regression suite: block_cells must be enforced on the pc-null fallback path.

    Previously all block_cells predicates were silently ignored when
    probability_classifier=None because evaluate_v8_champion_lgb_only() was
    called and its result returned before gate 9c could run.  Fix: the gate is
    now applied inside the pc-null branch after the fallback decision is known.
    """

    def _run(self, predicates, surface=None, extra_overrides=None):
        """Run evaluate_v9_ensemble with pc=None and the given block_cells predicates."""
        from strategies import gate_params as _gp
        from strategies.configs.v9_ensemble import evaluate_v9_ensemble

        if surface is None:
            surface = _make_surface_pc_null(**(extra_overrides or {}))

        with patch.object(_gp, "_ACTIVE") as mock_active:
            # Also enable fallback_to_lgb_on_pc_null so the branch fires.
            mock_active.get.return_value = {
                "block_cells": predicates,
                "fallback_to_lgb_on_pc_null": True,
            }
            return evaluate_v9_ensemble(surface)

    def test_block_cells_enforced_on_pc_null_fallback_trade(self):
        """CORE regression: block UP×T-61-90 while in pc-null fallback → must SKIP."""
        predicates = [{"direction": "UP", "t_band": "T-61-90", "conf_min": 0.25}]
        decision = self._run(predicates)
        assert decision.action == "SKIP", (
            f"Expected SKIP but got {decision.action}; "
            f"skip_reason={decision.skip_reason!r}. "
            "block_cells gate was not enforced on pc-null fallback path."
        )
        assert "block_cells" in (decision.skip_reason or ""), (
            f"skip_reason does not contain 'block_cells': {decision.skip_reason!r}"
        )

    def test_block_cells_skip_reason_contains_predicate_on_pc_null(self):
        """Skip reason must include matching predicate details (not just 'block_cells')."""
        predicates = [
            {"direction": "UP", "regime": "chop", "conf_min": 0.25},
        ]
        decision = self._run(predicates)
        assert decision.action == "SKIP"
        reason = decision.skip_reason or ""
        assert "block_cells" in reason
        assert "UP" in reason

    def test_non_matching_predicate_allows_fallback_on_pc_null(self):
        """A predicate that doesn't match must NOT cause a spurious SKIP."""
        # Block DOWN only — LGB says UP, so this predicate must not fire.
        predicates = [{"direction": "DOWN", "t_band": "T-61-90", "conf_min": 0.25}]
        decision = self._run(predicates)
        # The decision may be SKIP for other gate reasons (e.g. 3-tick
        # confirmation) but the skip_reason must NOT be block_cells.
        if decision.action == "SKIP":
            assert "block_cells" not in (decision.skip_reason or ""), (
                f"block_cells fired on wrong direction: {decision.skip_reason!r}"
            )

    def test_empty_block_cells_on_pc_null_does_not_skip(self):
        """Empty predicates → gate is no-op even in pc-null fallback."""
        decision = self._run([])
        if decision.action == "SKIP":
            assert "block_cells" not in (decision.skip_reason or ""), (
                f"Empty predicates caused block_cells skip: {decision.skip_reason!r}"
            )

    def test_cascade_down_conf_min_predicate_on_pc_null(self):
        """Reproduce prod scenario: DOWN×CASCADE×conf>=0.30 must be blocked.

        This exactly matches the predicates on v9_1_lgb_only that were being
        bypassed for trades #8137, #8126, #8125, #8121, #8096 (Hub note #434).
        """
        predicates = [
            {"direction": "DOWN", "regime": "CASCADE", "conf_min": 0.30},
        ]
        # Use a surface with CASCADE regime and DOWN LGB signal.
        surface = _make_surface_pc_null(
            regime="CASCADE",
            probability_lgb=0.18,   # DOWN direction, dist=0.32
            delta_chainlink=-0.0003,
            delta_tiingo=-0.0002,
            clob_down_ask=0.32,
        )
        decision = self._run(predicates, surface=surface)
        assert decision.action == "SKIP", (
            f"Prod predicate DOWN×CASCADE×conf>=0.30 was NOT enforced on pc-null "
            f"fallback (action={decision.action!r}, "
            f"skip_reason={decision.skip_reason!r}). "
            "This is the exact regression from Hub note #434."
        )
        assert "block_cells" in (decision.skip_reason or "")
