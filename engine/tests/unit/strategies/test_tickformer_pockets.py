"""Tests for multi-pocket (band-keyed threshold) support in _tickformer_base.

Covers:
  1. Single-pocket override (legacy) still works unchanged.
  2. pockets=[] also falls back to legacy single-pocket path.
  3. 2-pocket v17-style config fires correctly in each band at each threshold.
  4. Tick OUTSIDE both pocket bands → SKIP outside_eval_offset_remaining_band.
  5. Tick INSIDE a pocket band but probability below that pocket's threshold
     → SKIP conviction_below_threshold.
  6. Overlapping pockets → strictest wins, recorded in meta.
  7. down_threshold defaults to 1 - up_threshold per pocket independently.
  8. Malformed pocket entries are skipped; valid pockets in the same list still fire.
  9. remaining=None with pockets set → SKIP outside_eval_offset_remaining_band.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import _tickformer_base  # noqa: E402
from strategies.configs.tickformer_v17_sniper import (  # noqa: E402
    evaluate_tickformer_v17_sniper,
)
from strategies.configs.tickformer_v16_pure import (  # noqa: E402
    evaluate_tickformer_v16_pure,
)


@contextmanager
def _gp_active(params):
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _reset_state():
    _tickformer_base._consec_state.clear()
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v17_sniper")
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v16_pure")
    yield
    _tickformer_base._consec_state.clear()


def _surface(
    prob_field: str,
    prob_value,
    *,
    eval_offset: int = 150,
    trade_signal=None,
    asset: str = "BTC",
    fill_price: float = 0.75,
):
    ns = SimpleNamespace(
        asset=asset,
        window_ts=int(time.time_ns() % 1_000_000) + 1_779_000_000,
        eval_offset=eval_offset,
        tickformer_trade_signal=trade_signal,
        clob_implied_up=fill_price,
        fill_price=fill_price,
    )
    setattr(ns, prob_field, prob_value)
    return ns


# ── Helper pocket specs used across tests ────────────────────────────────────

# Mirrors the spec in the task description for v17:
#   pocket A: up=0.65, band [120, 179]
#   pocket B: up=0.70, band [60, 119]
_V17_POCKETS = [
    {"up_threshold": 0.65, "rem_min": 120, "rem_max": 179, "down_threshold": 0.35},
    {"up_threshold": 0.70, "rem_min": 60,  "rem_max": 119},
]


# ── 1. Legacy single-pocket path still works when pockets absent ──────────────


def test_legacy_single_pocket_no_pockets_key_fires():
    """No 'pockets' key → legacy path → TRADE on p >= up_threshold."""
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=100)
    with _gp_active({"shadow_only": 0, "up_threshold": 0.80, "eval_offset_remaining_min": 60, "eval_offset_remaining_max": 140}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert "pocket_index" not in dec.metadata


def test_legacy_single_pocket_below_threshold_skips():
    """Legacy path: p < up_threshold → conviction_below_threshold."""
    surface = _surface("probability_tickformer_v17", 0.75, eval_offset=100)
    with _gp_active({"shadow_only": 0, "up_threshold": 0.80, "eval_offset_remaining_min": 60, "eval_offset_remaining_max": 140}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


def test_legacy_single_pocket_outside_band_skips():
    """Legacy path: remaining < rem_min → outside_eval_offset_remaining_band."""
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=10)
    with _gp_active({"shadow_only": 0, "up_threshold": 0.80, "eval_offset_remaining_min": 60, "eval_offset_remaining_max": 140}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


# ── 2. pockets=[] falls back to legacy path ──────────────────────────────────


def test_empty_pockets_list_falls_back_to_legacy():
    """pockets=[] → legacy path (same as absent key)."""
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=100)
    with _gp_active({
        "pockets": [],
        "shadow_only": 0,
        "up_threshold": 0.80,
        "eval_offset_remaining_min": 60,
        "eval_offset_remaining_max": 140,
    }):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert "pocket_index" not in dec.metadata


# ── 3. Two-pocket v17 config fires correctly in each band ────────────────────


def test_two_pocket_fires_in_pocket_A_band():
    """p=0.68 >= pocket_A.up_threshold=0.65, remaining=150 ∈ [120,179] → UP TRADE."""
    surface = _surface("probability_tickformer_v17", 0.68, eval_offset=150)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert dec.metadata["pocket_index"] == 0
    assert dec.metadata["pocket_spec"]["up_threshold"] == 0.65
    assert dec.metadata["pocket_spec"]["rem_min"] == 120
    assert dec.metadata["pocket_spec"]["rem_max"] == 179


def test_two_pocket_fires_in_pocket_B_band():
    """p=0.72 >= pocket_B.up_threshold=0.70, remaining=90 ∈ [60,119] → UP TRADE."""
    surface = _surface("probability_tickformer_v17", 0.72, eval_offset=90)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert dec.metadata["pocket_index"] == 1
    assert dec.metadata["pocket_spec"]["up_threshold"] == 0.70


def test_two_pocket_pocket_A_prob_too_low_for_pocket_A_but_B_not_applicable():
    """p=0.68 >= pocket_A threshold(0.65), but remaining=90 is in pocket_B band(60-119).
    pocket_B threshold=0.70, p=0.68 < 0.70 → conviction_below_threshold."""
    surface = _surface("probability_tickformer_v17", 0.68, eval_offset=90)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


# ── 4. Tick OUTSIDE all pocket bands → SKIP outside_band ─────────────────────


def test_two_pocket_remaining_above_all_bands():
    """remaining=200, pockets cover [60,119] and [120,179] → outside."""
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=200)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


def test_two_pocket_remaining_below_all_bands():
    """remaining=20 < rem_min=60 of both pockets → outside."""
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=20)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


def test_two_pocket_remaining_in_gap_between_bands():
    """Gap between pocket bands: remaining=50 is between bands [60-119] and [120-179].
    Use pockets that have a gap: [130,179] and [60,119]."""
    pockets_with_gap = [
        {"up_threshold": 0.65, "rem_min": 130, "rem_max": 179},
        {"up_threshold": 0.70, "rem_min": 60,  "rem_max": 119},
    ]
    # remaining=125 falls in the gap [120,129]
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=125)
    with _gp_active({"pockets": pockets_with_gap, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


# ── 5. In-band but below threshold → conviction_below_threshold ───────────────


def test_two_pocket_in_band_below_up_threshold():
    """p=0.60 < pocket_A.up_threshold=0.65 and p > pocket_A.down_threshold=0.35
    → in-band but neither UP nor DOWN threshold crossed."""
    surface = _surface("probability_tickformer_v17", 0.60, eval_offset=150)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


def test_two_pocket_in_band_below_down_threshold():
    """p=0.40 > pocket_A.down_threshold=0.35 → above down but below up → conviction_below_threshold."""
    surface = _surface("probability_tickformer_v17", 0.40, eval_offset=150)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


def test_two_pocket_down_fires_via_explicit_down_threshold():
    """p=0.30 <= pocket_A.down_threshold=0.35, remaining=150 → DOWN TRADE."""
    surface = _surface(
        "probability_tickformer_v17", 0.30, eval_offset=150, fill_price=0.20
    )
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "DOWN"
    assert dec.metadata["pocket_index"] == 0


# ── 6. Overlapping pockets → strictest wins ───────────────────────────────────


def test_overlapping_pockets_strictest_up_threshold_wins():
    """Two overlapping pockets both contain remaining=150.
    p=0.75 crosses both up_thresholds (0.65 and 0.70).
    Strictest UP = highest up_threshold = 0.70 wins."""
    overlapping = [
        {"up_threshold": 0.65, "rem_min": 100, "rem_max": 200},  # idx=0
        {"up_threshold": 0.70, "rem_min": 100, "rem_max": 200},  # idx=1
    ]
    surface = _surface("probability_tickformer_v17", 0.75, eval_offset=150)
    with _gp_active({"pockets": overlapping, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert dec.metadata["pocket_index"] == 1
    assert dec.metadata["pocket_spec"]["up_threshold"] == 0.70


def test_overlapping_pockets_only_lower_threshold_crossed():
    """Two overlapping pockets, p crosses only the lower threshold.
    p=0.67 >= 0.65 but p < 0.70 → only pocket_0 qualifies → fires with pocket_0."""
    overlapping = [
        {"up_threshold": 0.65, "rem_min": 100, "rem_max": 200},  # idx=0
        {"up_threshold": 0.70, "rem_min": 100, "rem_max": 200},  # idx=1
    ]
    surface = _surface("probability_tickformer_v17", 0.67, eval_offset=150)
    with _gp_active({"pockets": overlapping, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert dec.metadata["pocket_index"] == 0


def test_overlapping_pockets_strictest_down_threshold_wins():
    """Two overlapping DOWN pockets; strictest = lowest down_threshold."""
    overlapping = [
        {"up_threshold": 0.80, "rem_min": 100, "rem_max": 200, "down_threshold": 0.25},  # idx=0
        {"up_threshold": 0.80, "rem_min": 100, "rem_max": 200, "down_threshold": 0.20},  # idx=1
    ]
    # p=0.18 <= 0.20 (both) → strictest = idx=1 (0.20 < 0.25)
    surface = _surface(
        "probability_tickformer_v17", 0.18, eval_offset=150, fill_price=0.15
    )
    with _gp_active({"pockets": overlapping, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "DOWN"
    assert dec.metadata["pocket_index"] == 1
    assert dec.metadata["pocket_spec"]["down_threshold"] == 0.20


# ── 7. down_threshold defaults to 1 - up_threshold per pocket ────────────────


def test_pocket_down_threshold_defaults_to_1_minus_up():
    """Pocket without down_threshold: down defaults to 1 - up_threshold."""
    pockets = [
        {"up_threshold": 0.72, "rem_min": 60, "rem_max": 180},
    ]
    normed = _tickformer_base._normalise_pocket(pockets[0], 0)
    assert normed is not None
    # down_threshold should be 1 - 0.72 = 0.28
    assert abs(normed["down_threshold"] - 0.28) < 1e-9


def test_pocket_down_threshold_explicit_overrides_default():
    """Explicit down_threshold in pocket is preserved."""
    raw = {"up_threshold": 0.72, "rem_min": 60, "rem_max": 180, "down_threshold": 0.15}
    normed = _tickformer_base._normalise_pocket(raw, 0)
    assert normed is not None
    assert normed["down_threshold"] == 0.15


def test_pocket_down_fires_with_default_down_threshold():
    """p <= (1 - up_threshold) triggers DOWN with default down_threshold."""
    pockets = [
        # up=0.72 → default down=0.28
        {"up_threshold": 0.72, "rem_min": 60, "rem_max": 180},
    ]
    # p=0.25 <= 0.28 → DOWN
    surface = _surface(
        "probability_tickformer_v17", 0.25, eval_offset=120, fill_price=0.20
    )
    with _gp_active({"pockets": pockets, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "DOWN"


# ── 8. Malformed pocket entries skipped; valid ones still fire ────────────────


def test_malformed_pocket_skipped_valid_pocket_fires():
    """A malformed pocket (missing rem_max) is skipped; valid pocket still fires."""
    pockets = [
        {"up_threshold": 0.80, "rem_min": 60},  # malformed: missing rem_max
        {"up_threshold": 0.65, "rem_min": 60, "rem_max": 180},  # valid
    ]
    surface = _surface("probability_tickformer_v17", 0.70, eval_offset=120)
    with _gp_active({"pockets": pockets, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    # Valid pocket is at original index 1, but after filtering it's at position 0
    # in the valid_pockets list, so pocket_index should be 0.
    assert dec.metadata["pocket_index"] == 0


def test_malformed_pocket_invalid_band_skipped():
    """Pocket with rem_min > rem_max is invalid and skipped."""
    raw = {"up_threshold": 0.80, "rem_min": 200, "rem_max": 100}
    result = _tickformer_base._normalise_pocket(raw, 0)
    assert result is None


def test_all_malformed_pockets_falls_to_empty_which_uses_legacy():
    """If all pockets are malformed, valid_pockets=[] → legacy path fires."""
    pockets = [
        {"up_threshold": 0.80, "rem_min": 60},  # malformed
    ]
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=100)
    with _gp_active({
        "pockets": pockets,
        "shadow_only": 0,
        # legacy params active as fallback
        "up_threshold": 0.80,
        "eval_offset_remaining_min": 60,
        "eval_offset_remaining_max": 140,
    }):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert "pocket_index" not in dec.metadata


# ── 9. remaining=None with pockets → outside_band ────────────────────────────


def test_remaining_none_with_pockets_skips_outside_band():
    """If eval_offset is None, remaining=None → outside_eval_offset_remaining_band."""
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=None)
    # Remove eval_offset entirely to make remaining=None
    del surface.eval_offset
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


# ── 10. Pocket meta keys are present and correct ─────────────────────────────


def test_pocket_meta_keys_present_on_trade():
    """When pocket fires, meta must include pocket_index and pocket_spec."""
    surface = _surface("probability_tickformer_v17", 0.72, eval_offset=90)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert "pocket_index" in dec.metadata
    assert "pocket_spec" in dec.metadata
    spec = dec.metadata["pocket_spec"]
    assert "up_threshold" in spec
    assert "down_threshold" in spec
    assert "rem_min" in spec
    assert "rem_max" in spec


def test_pocket_meta_up_down_thresholds_match_winning_pocket():
    """meta.up_threshold / down_threshold should reflect winning pocket, not legacy."""
    surface = _surface("probability_tickformer_v17", 0.72, eval_offset=90)
    with _gp_active({"pockets": _V17_POCKETS, "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    # pocket B: up=0.70, down defaults to 0.30, band [60,119]
    assert dec.metadata["up_threshold"] == 0.70
    assert abs(dec.metadata["down_threshold"] - 0.30) < 1e-9


# ── 11. Shadow-only kill switch works with pockets ───────────────────────────


def test_pocket_shadow_only_skips_with_would_trade():
    """shadow_only=1 (default) with pockets → SKIP shadow_only_no_trade."""
    surface = _surface("probability_tickformer_v17", 0.72, eval_offset=90)
    # Default shadow_only=1
    with _gp_active({"pockets": _V17_POCKETS}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade"
    assert dec.metadata["would_trade"] is True
    assert dec.metadata["pocket_index"] == 1


# ── 12. _normalise_pocket unit tests ─────────────────────────────────────────


def test_normalise_pocket_valid_with_explicit_down():
    raw = {"up_threshold": 0.65, "rem_min": 120, "rem_max": 179, "down_threshold": 0.35}
    normed = _tickformer_base._normalise_pocket(raw, 0)
    assert normed == {"up_threshold": 0.65, "down_threshold": 0.35, "rem_min": 120, "rem_max": 179}


def test_normalise_pocket_valid_without_down():
    raw = {"up_threshold": 0.70, "rem_min": 60, "rem_max": 119}
    normed = _tickformer_base._normalise_pocket(raw, 1)
    assert normed is not None
    assert normed["up_threshold"] == 0.70
    assert abs(normed["down_threshold"] - 0.30) < 1e-9
    assert normed["rem_min"] == 60
    assert normed["rem_max"] == 119


def test_normalise_pocket_missing_up_threshold():
    raw = {"rem_min": 60, "rem_max": 119}
    assert _tickformer_base._normalise_pocket(raw, 0) is None


def test_normalise_pocket_missing_rem_min():
    raw = {"up_threshold": 0.70, "rem_max": 119}
    assert _tickformer_base._normalise_pocket(raw, 0) is None


def test_normalise_pocket_inverted_band():
    raw = {"up_threshold": 0.70, "rem_min": 200, "rem_max": 100}
    assert _tickformer_base._normalise_pocket(raw, 0) is None


# ── 13. _match_pocket unit tests ─────────────────────────────────────────────


def _build_pockets():
    return [
        _tickformer_base._normalise_pocket(pk, i)
        for i, pk in enumerate(_V17_POCKETS)
    ]


def test_match_pocket_in_band_A_up():
    pockets = _build_pockets()
    direction, idx, spec = _tickformer_base._match_pocket(pockets, 150, 0.68)
    assert direction == "UP"
    assert idx == 0
    assert spec["up_threshold"] == 0.65


def test_match_pocket_in_band_B_up():
    pockets = _build_pockets()
    direction, idx, spec = _tickformer_base._match_pocket(pockets, 90, 0.72)
    assert direction == "UP"
    assert idx == 1
    assert spec["up_threshold"] == 0.70


def test_match_pocket_outside_all_bands():
    pockets = _build_pockets()
    direction, idx, spec = _tickformer_base._match_pocket(pockets, 200, 0.95)
    assert direction is None
    assert idx is None
    assert spec is None


def test_match_pocket_in_band_below_threshold():
    pockets = _build_pockets()
    # remaining=150 in band A [120,179], p=0.60 < 0.65 (up) and > 0.35 (down)
    direction, idx, spec = _tickformer_base._match_pocket(pockets, 150, 0.60)
    assert direction == "__in_band_no_cross__"
    assert idx is None
    assert spec is None


def test_match_pocket_in_band_A_down():
    pockets = _build_pockets()
    # pocket_A explicit down_threshold=0.35; p=0.30 <= 0.35
    direction, idx, spec = _tickformer_base._match_pocket(pockets, 150, 0.30)
    assert direction == "DOWN"
    assert idx == 0
