"""v9_5_eth_blend_down_mid — DOWN-MID conviction tier of the v9.5 ETH
BLEND ladder (GHOST).

Fires DOWN when probability_lgb_v9_5_eth falls inside the band
(down_threshold_min, down_threshold] — default (0.12, 0.18]. See sibling
v9_5_eth_blend_up_low.py for the full ladder layout.

Band (0.12, 0.18]: n=196 OOF, mean WR ~89.7%, Wilson LB ~85%.

DOWN-only by design. Asset: ETH only. Reads the EXISTING
probability_lgb_v9_5_eth column. GHOST mode by default. $5
max_position_usd.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_eth_blend_down_mid"
_VERSION = "1.0.0"

# Band (0.12, 0.18]: n=196 OOF, mean WR ~89.7%, Wilson LB ~85%.
_DEFAULT_DOWN_THRESHOLD = 0.18
_DEFAULT_DOWN_THRESHOLD_MIN = 0.12

_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 240

_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Module-local consecutive-tick state — isolated from sibling tiers.
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


def evaluate_v9_5_eth_blend_down_mid(surface: "FullDataSurface") -> StrategyDecision:
    p_eth = getattr(surface, "probability_lgb_v9_5_eth", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_eth is None:
        return _skip(
            "v9_5_eth_model_not_loaded",
            {"probability_lgb_v9_5_eth": None},
        )

    p_eth = float(p_eth)

    down_threshold = _gp.get_float(
        "down_threshold", None, _DEFAULT_DOWN_THRESHOLD
    )
    down_threshold_min = _gp.get_float(
        "down_threshold_min", None, _DEFAULT_DOWN_THRESHOLD_MIN
    )
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_eth": p_eth,
        "eval_offset": eval_offset,
        "down_threshold": down_threshold,
        "down_threshold_min": down_threshold_min,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # DOWN-only band check: (down_threshold_min, down_threshold].
    if down_threshold_min < p_eth <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("outside_conviction_band", meta)

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

    confidence_score = float(abs(p_eth - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float("collateral_pct", None, _DEFAULT_COLLATERAL_PCT)
    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct
    meta["gtc_cap"] = gtc_cap
    meta["strategy_id"] = _STRATEGY_ID
    meta["strategy_version"] = _VERSION

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
        entry_reason="v9_5_eth_blend_down_mid_pass",
        skip_reason=None,
        metadata=meta,
    )
