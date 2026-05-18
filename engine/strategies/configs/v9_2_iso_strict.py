"""v9_2_iso_strict — iso-calibrated v9.2 at high-conviction thresholds.

High-conviction variant: fires only at calibrated probability >= 0.85 (UP)
or <= 0.12 (DOWN). At these levels the iso curve indicates near-certainty
(raw 0.85+ → calibrated ~1.0, hub note #536 iso curve table).

Expected: ~0.5× fires/day vs v9_2_iso_expand, ~95% projected WR.
This is the "survival variant" — useful if iso_expand suffers from regime
drift or the tick→trade conversion is unfavorable.

The DOWN threshold 0.12 (vs 0.098 in volmatch) corresponds to an even
tighter calibration band, selecting only the most strongly DOWN signals.

Enabled hours: exclude 09 and 15 UTC (per-hour WR cliffs, hub note #524).
Single qualifying tick (N=1), eval_offset [60, 210]. Same structure as
v9_2_raw_lgb, no veto layers, no cohort gates.

GHOST mode by default — Billy promotes manually per feedback_no_auto_promote.md.
Hub notes #536 (iso findings + #531 tightening analysis), session ~#538.
Companion timesfm PR: feat/v9_2_post_iso_layer.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_iso_strict"
_VERSION = "1.0.0"

# High-conviction thresholds on the calibrated probability.
# UP >= 0.85: iso curve maps raw ~0.72-0.75 to ~0.80, raw 0.85+ → ~1.0
# DOWN <= 0.12: tighter than volmatch 0.098, selects most confident DOWN signals
_DEFAULT_UP_THRESHOLD = 0.85
_DEFAULT_DOWN_THRESHOLD = 0.12
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210
_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
_DEFAULT_MIN_CONSEC_TICKS = 1

# Blocked UTC hours: 09 and 15 — per-hour WR cliffs (hub note #524, bg-agent #2).
_DEFAULT_BLOCKED_HOURS: list[int] = [9, 15]

_consec_state: dict[tuple[int, str], tuple[int, float]] = {}
_MAX_GAP_S = 5.0


def _bump_and_check(window_ts: int, direction: str, min_ticks: int) -> int:
    now = time.time()
    key = (int(window_ts), direction)
    other = "DOWN" if direction == "UP" else "UP"
    _consec_state.pop((int(window_ts), other), None)
    prev = _consec_state.get(key)
    if prev is None or (now - prev[1]) > _MAX_GAP_S:
        count = 1
    else:
        count = prev[0] + 1
    _consec_state[key] = (count, now)
    if len(_consec_state) > 50:
        cutoff = now - 600.0
        for k in [k for k, v in _consec_state.items() if v[1] < cutoff]:
            _consec_state.pop(k, None)
    return count


def _skip(reason: str, metadata: dict) -> StrategyDecision:
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=0.0,
        entry_cap=0.0,
        collateral_pct=0.0,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="",
        skip_reason=reason,
        metadata=metadata,
    )


def evaluate_v9_2_iso_strict(surface: "FullDataSurface") -> StrategyDecision:
    p_iso = getattr(surface, "probability_lgb_v9_2_post_iso", None)
    eval_offset = getattr(surface, "eval_offset", None)
    hour_utc = getattr(surface, "hour_utc", None)

    if p_iso is None:
        return _skip("v9_2_post_iso_not_loaded", {"probability_lgb_v9_2_post_iso": None})

    p_iso = float(p_iso)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)
    blocked_hours = _gp.get_list("blocked_hours_utc", None, _DEFAULT_BLOCKED_HOURS)

    meta = {
        "probability_lgb_v9_2_post_iso": p_iso,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
    }

    if hour_utc is not None and hour_utc in blocked_hours:
        return _skip(f"blocked_utc_hour_{hour_utc}", meta)

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_iso >= up_threshold:
        direction = "UP"
    elif p_iso <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    window_ts = getattr(surface, "window_ts", None)
    min_consec = _gp.get_int(
        "min_consecutive_pass_ticks", None, _DEFAULT_MIN_CONSEC_TICKS
    )
    consec_count = _bump_and_check(window_ts or 0, direction, min_consec)
    meta["consec_tick_count"] = consec_count
    meta["min_consecutive_pass_ticks"] = min_consec
    if consec_count < min_consec:
        return _skip(
            f"awaiting_consec_ticks ({consec_count}/{min_consec})",
            meta,
        )

    confidence_score = float(abs(p_iso - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float("collateral_pct", None, _DEFAULT_COLLATERAL_PCT)
    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct
    meta["gtc_cap"] = gtc_cap

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence,
        confidence_score=confidence_score,
        entry_cap=entry_cap,
        collateral_pct=collateral_pct,
        gtc_cap=gtc_cap,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="v9_2_iso_strict_pass",
        skip_reason=None,
        metadata=meta,
    )
