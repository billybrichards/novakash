"""v9_1_lgb_only — clean raw v9.1 signal strategy.

Restored 2026-05-14 to a clean GHOST observation strategy modelled on
``v9_2_raw_lgb``. Fires purely on ``probability_lgb_v9_1`` — no
v9_ensemble base-gate delegation, no cohort gates, no sister-pair veto,
no blocked hours, no cell pauses.

Criteria (default thresholds — tunable via runtime overrides):
  - UP   when probability_lgb_v9_1 >= 0.70
  - DOWN when probability_lgb_v9_1 <= 0.30
  - eval_offset in [60, 210]  (T-60 to T-210 — sec-to-close convention)
  - one qualifying tick is enough (no N-of-M cohort gate)

If ``probability_lgb_v9_1`` is None (timesfm-service has V9_1_ENABLED=false
or the model failed to load), SKIPs with ``v9_1_model_not_loaded``.

GHOST mode by default — Billy promotes manually.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_1_lgb_only"
_VERSION = "1.0.0"

_DEFAULT_UP_THRESHOLD = 0.70
_DEFAULT_DOWN_THRESHOLD = 0.30
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210
_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.80
_DEFAULT_MIN_CONSEC_TICKS = 1

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
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


def evaluate_v9_1_lgb_only(surface: "FullDataSurface") -> StrategyDecision:
    p_v91 = getattr(surface, "probability_lgb_v9_1", None)
    eval_offset = getattr(surface, "eval_offset", None)

    if p_v91 is None:
        return _skip(
            "v9_1_model_not_loaded",
            {"probability_lgb_v9_1": None, "v9_1_enabled": False},
        )

    p_v91 = float(p_v91)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_1": p_v91,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_v91 >= up_threshold:
        direction = "UP"
    elif p_v91 <= down_threshold:
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

    confidence_score = float(abs(p_v91 - 0.5) * 2.0)
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
        entry_reason="v9_1_raw_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
