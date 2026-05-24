"""v9_2_v12_AND_ghost — AND-filter ensemble (v9.2 + v12), BTC 5m, GHOST.

Fires only when BOTH probability_lgb_v9_2 AND probability_lgb_v12 agree on
direction at the audit-recommended thresholds. Sister strategy to the LIVE
v9_2_v12_combo (which uses tighter 0.75/0.70 UP, 0.20/0.40 DOWN); this
GHOST variant uses the audit's optimal Wilson-LB op-point.

Asset: BTC only. Strategy will SKIP on any non-BTC surface (defensive guard
— both v9.2 and v12 are BTC-calibrated).

Criteria (RDS note #618 — 14d filter-mode + AND-filter audit):
  - UP   when p_v9_2 >= 0.725 AND p_v12 >= 0.750
          -> 92.1% WR on n=369 windows (Wilson LB 88.9%)
  - DOWN when p_v9_2 <= 0.250 AND p_v12 <= 0.200
          -> 81.7% WR on n=202 windows (Wilson LB 75.8%)
  - eval_offset in [60, 210]  (T-60 to T-210)
  - min_consecutive_pass_ticks=1

Why it works: v9.2 (LIVE BTC model) and v12 (separate ensemble) are diverse
signals — when BOTH agree, calibration is much tighter than either alone.
The simpler AND-filter beats v12_meta_gate by 8pp.

Both probability columns ALREADY emit (no new probability emission needed).
This module is pure strategy wiring.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window.

Sibling strategy: v9_2_v12_combo (LIVE at tighter thresholds).
Sibling strategy: v12_meta_gate (GHOST — beaten by 8pp by this filter).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_v12_AND_ghost"
_VERSION = "1.0.0"

# Audit operating point per RDS note #618 (14d, n=369 UP / n=202 DOWN).
_DEFAULT_UP_V92_THRESHOLD = 0.725
_DEFAULT_UP_V12_THRESHOLD = 0.750
_DEFAULT_DOWN_V92_THRESHOLD = 0.25
_DEFAULT_DOWN_V12_THRESHOLD = 0.20

# Matches v9_2_raw_lgb / v9_2_v12_combo band — like-for-like comparison.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210

_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Module-local so this strategy's consecutive-tick state cannot collide with
# sibling strategies (v9_2_v12_combo, v9_2_raw_lgb, v9_3_btc_*).
_consec_state: dict[tuple[int, str], tuple[int, float]] = {}
_MAX_GAP_S = 5.0


def _bump_and_check(window_ts: int, direction: str, min_ticks: int) -> int:
    """Return current consecutive-pass tick count for (window_ts, direction).

    Caller decides whether count >= min_ticks (fire) or < min_ticks (skip).
    Reset semantics: any direction change for the same window OR a gap > 5s
    between qualifying ticks resets the counter to 1.
    """
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


def evaluate_v9_2_v12_AND_ghost(surface: "FullDataSurface") -> StrategyDecision:
    p_v92 = getattr(surface, "probability_lgb_v9_2", None)
    p_v12 = getattr(surface, "probability_lgb_v12", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard — both v9.2 and v12 are BTC-calibrated. Refuse
    # to fire on a non-BTC surface even if the registry ever wires one in.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_v92 is None:
        return _skip("v9_2_model_not_loaded", {"probability_lgb_v9_2": None})
    if p_v12 is None:
        return _skip("v12_model_not_loaded", {"probability_lgb_v12": None})

    p_v92 = float(p_v92)
    p_v12 = float(p_v12)

    up_v92 = _gp.get_float("up_v92_threshold", None, _DEFAULT_UP_V92_THRESHOLD)
    up_v12 = _gp.get_float("up_v12_threshold", None, _DEFAULT_UP_V12_THRESHOLD)
    down_v92 = _gp.get_float(
        "down_v92_threshold", None, _DEFAULT_DOWN_V92_THRESHOLD
    )
    down_v12 = _gp.get_float(
        "down_v12_threshold", None, _DEFAULT_DOWN_V12_THRESHOLD
    )
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_2": p_v92,
        "probability_lgb_v12": p_v12,
        "eval_offset": eval_offset,
        "up_v92_threshold": up_v92,
        "up_v12_threshold": up_v12,
        "down_v92_threshold": down_v92,
        "down_v12_threshold": down_v12,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_v92 >= up_v92 and p_v12 >= up_v12:
        direction = "UP"
    elif p_v92 <= down_v92 and p_v12 <= down_v12:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks).
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

    # Use the stronger signal for confidence scoring (matches v9_2_v12_combo).
    confidence_score = float(max(abs(p_v92 - 0.5), abs(p_v12 - 0.5)) * 2.0)
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
        entry_reason="v9_2_v12_AND_ghost_pass",
        skip_reason=None,
        metadata=meta,
    )
