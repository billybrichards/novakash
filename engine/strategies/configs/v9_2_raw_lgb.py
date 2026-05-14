"""v9_2_raw_lgb — clean raw v9.2 signal strategy.

Fires on raw probability_lgb_v9_2 without v9_ensemble base-gate delegation.
No cohort gates, no sister-pair veto, no cell pauses, no blocked hours.

Criteria (Hub note #508, 30d raw signal matrix on v9_2_super_lgb_only):
  - UP   when probability_lgb_v9_2 >= 0.75  (93-94% WR at n=126-143)
  - DOWN when probability_lgb_v9_2 <= 0.20  (91% WR at n=23-55)
  - eval_offset in [60, 210]  (T-60 to T-210 — sec-to-close convention)
  - One qualifying tick is enough (no N-of-M cohort gate)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_raw_lgb"
_VERSION = "1.0.0"

_DEFAULT_UP_THRESHOLD = 0.75
_DEFAULT_DOWN_THRESHOLD = 0.20
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210
_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025


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


def evaluate_v9_2_raw_lgb(surface: "FullDataSurface") -> StrategyDecision:
    p_v92 = getattr(surface, "probability_lgb_v9_2", None)
    eval_offset = getattr(surface, "eval_offset", None)

    if p_v92 is None:
        return _skip("v9_2_model_not_loaded", {"probability_lgb_v9_2": None})

    p_v92 = float(p_v92)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_2": p_v92,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_v92 >= up_threshold:
        direction = "UP"
    elif p_v92 <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    confidence_score = float(abs(p_v92 - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float("collateral_pct", None, _DEFAULT_COLLATERAL_PCT)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence,
        confidence_score=confidence_score,
        entry_cap=entry_cap,
        collateral_pct=collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="v9_2_raw_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
