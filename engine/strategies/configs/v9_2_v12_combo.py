"""v9_2_v12_combo — intersection of v9.2 and v12 LGB signals.

Fires when BOTH probability_lgb_v9_2 AND probability_lgb_v12 agree on direction.
No cohort gates, no sister-pair veto, no cell pauses, no blocked hours.

Criteria (5d signal_evaluations intersection analysis):
  - UP   when p_v9_2 >= 0.75 AND p_v12 >= 0.70  -> 95.9% WR (~13 fires/day)
  - DOWN when p_v9_2 <= 0.20 AND p_v12 <= 0.40  -> 96.0% WR (~9 fires/day)
  - eval_offset in [60, 210]  (T-60 to T-210 — sec-to-close convention)
  - One qualifying tick is enough (no N-of-M cohort gate)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — A/B compare vs v9_2_raw_lgb before promotion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_v12_combo"
_VERSION = "1.0.0"

_DEFAULT_UP_V92_THRESHOLD = 0.75
_DEFAULT_UP_V12_THRESHOLD = 0.70
_DEFAULT_DOWN_V92_THRESHOLD = 0.20
_DEFAULT_DOWN_V12_THRESHOLD = 0.40
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210
_DEFAULT_GTC_CAP = 0.90


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


def evaluate_v9_2_v12_combo(surface: "FullDataSurface") -> StrategyDecision:
    p_v92 = getattr(surface, "probability_lgb_v9_2", None)
    p_v12 = getattr(surface, "probability_lgb_v12", None)
    eval_offset = getattr(surface, "eval_offset", None)

    if p_v92 is None:
        return _skip("v9_2_model_not_loaded", {"probability_lgb_v9_2": None})
    if p_v12 is None:
        return _skip("v12_model_not_loaded", {"probability_lgb_v12": None})

    p_v92 = float(p_v92)
    p_v12 = float(p_v12)

    up_v92 = _gp.get_float("up_v92_threshold", None, _DEFAULT_UP_V92_THRESHOLD)
    up_v12 = _gp.get_float("up_v12_threshold", None, _DEFAULT_UP_V12_THRESHOLD)
    down_v92 = _gp.get_float("down_v92_threshold", None, _DEFAULT_DOWN_V92_THRESHOLD)
    down_v12 = _gp.get_float("down_v12_threshold", None, _DEFAULT_DOWN_V12_THRESHOLD)
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
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_v92 >= up_v92 and p_v12 >= up_v12:
        direction = "UP"
    elif p_v92 <= down_v92 and p_v12 <= down_v12:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # Use the stronger signal for confidence scoring
    confidence_score = float(max(abs(p_v92 - 0.5), abs(p_v12 - 0.5)) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["gtc_cap"] = gtc_cap

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence,
        confidence_score=confidence_score,
        entry_cap=None,
        collateral_pct=None,
        gtc_cap=gtc_cap,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="v9_2_v12_combo_pass",
        skip_reason=None,
        metadata=meta,
    )
