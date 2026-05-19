"""v_eth_15m_classifier_strict — tightened ETH 15m classifier strategy.

Fires only when |probability_classifier - 0.5| >= conf_distance_min (default 0.25):
  - UP   when probability_classifier >= 0.75
  - DOWN when probability_classifier <= 0.25

Per Hub note #546 ghost backtest (Gamma-resolved, 19.4d):
  UP   at conf>=0.25: 96.2% WR n=52
  DOWN at conf>=0.25: 88.9% WR n=36
  UP   at conf>=0.20: 91.5% WR n=94

This hook reads ``probability_classifier`` (TimesFM Path1 classifier head)
directly off the data surface — same value that has been emitted to
``strategy_decisions.metadata_json.probability_classifier`` for the past
~19 days but was previously unreadable by the gate-driven classifier
strategies. The companion fix in ``gates/confidence.py`` also unblocks the
four existing v_eth/v_xrp × top10/top20 classifier strategies (Hub #546).

Defaults GHOST — Billy promotes manually per feedback_no_auto_promote.md.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v_eth_15m_classifier_strict"
_VERSION = "1.0.0"

_DEFAULT_CONF_DISTANCE_MIN = 0.25
_DEFAULT_EVAL_OFFSET_MIN = 60   # sec-to-close (T-minus)
_DEFAULT_EVAL_OFFSET_MAX = 450  # half the 15m window
_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
_DEFAULT_MIN_CONSEC_TICKS = 2
_DEFAULT_BLOCKED_HOURS: list[int] = []

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
_consec_state: dict[tuple[int, str], tuple[int, float]] = {}
_MAX_GAP_S = 5.0


def _bump_and_check(window_ts: int, direction: str, min_ticks: int) -> int:
    """Return current consecutive-pass tick count for (window_ts, direction)."""
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


def evaluate_v_eth_15m_classifier_strict(
    surface: "FullDataSurface",
) -> StrategyDecision:
    p_cls = getattr(surface, "probability_classifier", None)
    eval_offset = getattr(surface, "eval_offset", None)
    hour_utc = getattr(surface, "hour_utc", None)

    if p_cls is None:
        return _skip(
            "probability_classifier_not_available",
            {"probability_classifier": None},
        )

    p_cls = float(p_cls)

    conf_min = _gp.get_float(
        "conf_distance_min", None, _DEFAULT_CONF_DISTANCE_MIN
    )
    # Direction-specific overrides fall back to conf_distance_min when unset.
    up_conf_min = _gp.get_float("up_conf_distance_min", None, conf_min)
    down_conf_min = _gp.get_float("down_conf_distance_min", None, conf_min)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)
    blocked_hours = _gp.get_list("blocked_hours_utc", _DEFAULT_BLOCKED_HOURS)

    conf_dist = abs(p_cls - 0.5)
    direction = "UP" if p_cls > 0.5 else "DOWN"
    side_min = up_conf_min if direction == "UP" else down_conf_min
    meta = {
        "probability_classifier": p_cls,
        "conf_distance": conf_dist,
        "conf_distance_min": conf_min,
        "up_conf_distance_min": up_conf_min,
        "down_conf_distance_min": down_conf_min,
        "side_threshold": side_min,
        "eval_offset": eval_offset,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
    }

    if hour_utc is not None and hour_utc in blocked_hours:
        return _skip(f"blocked_utc_hour_{hour_utc}", meta)

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if conf_dist < side_min:
        return _skip("conviction_below_threshold", meta)

    # N-consecutive-tick confirmation gate
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

    confidence_score = float(conf_dist * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float(
        "collateral_pct", None, _DEFAULT_COLLATERAL_PCT
    )
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
        entry_reason="v_eth_15m_classifier_strict_pass",
        skip_reason=None,
        metadata=meta,
    )
