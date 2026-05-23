"""v9_5_xrp_tight — high-precision XRP 5m strategy reading v9.5 XRP LGB (GHOST).

Sibling of v9_5_xrp_raw_lgb. Reads the SAME probability_lgb_v9_5_xrp signal
but at the tight-corner operating point (UP p>=0.95 / DOWN p<=0.05) and
constrained to the late-window band (eval_offset in [120, 240]).

XRP's tight corner does NOT reach the v9.3 BTC 90%+ ceiling — UP-side
peaks at ~83% WR at thr=0.97 (n=263) and DOWN reaches 93% only at thr<=0.005
(n=241, too close to a degenerate point). The 0.95/0.05 corner gives
80.9% UP / 83.1% DOWN with reasonable volume (n=282 / n=319).

No v9_ensemble base-gate delegation. No cohort gates, no sister-pair veto,
no cell pauses, no blocked hours. Co-exists with v9_5_xrp_raw_lgb — both
read the same probability field but have independent consecutive-tick state
and DIFFERENT operating points.

Asset: XRP only. Strategy will SKIP on any non-XRP surface (defensive guard).

Criteria (timesfm PR #160 + /tmp/v9_5_xrp_strategy_design.md walk-forward CV):
  - UP   when probability_lgb_v9_5_xrp >= 0.95 -> 80.9% WR on 282 windows
  - DOWN when probability_lgb_v9_5_xrp <= 0.05 -> 83.1% WR on 319 windows
  - eval_offset in [120, 240] (late-window subsegment — tight-corner fires
    concentrate at stc 104-105s, eval_offset ~195s)
  - min_consecutive_pass_ticks=1

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window.

Timesfm-repo PR #160 — v9.5 XRP emission + production training script.
Strategy design doc: /tmp/v9_5_xrp_strategy_design.md
Engine precedent: v9_3_btc_tight (commit 5e8c147).
Sibling strategy in this PR: v9_5_xrp_raw_lgb (drop-in moderate).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_xrp_tight"
_VERSION = "1.0.0"

# Walk-forward CV tight-corner operating point.
_DEFAULT_UP_THRESHOLD = 0.95
_DEFAULT_DOWN_THRESHOLD = 0.05

# Late-window band — XRP tight-corner fires concentrate at stc 104-105s
# (eval_offset ~195s). q25 first-fire is at stc 45-75s (eval_offset 225-255s).
_DEFAULT_EVAL_OFFSET_MIN = 120
_DEFAULT_EVAL_OFFSET_MAX = 240

_DEFAULT_ENTRY_CAP = 0.95
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.95
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "XRP"

# Module-local state — independent from v9_5_xrp_raw_lgb's state even
# though both strategies read the same probability column.
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


def evaluate_v9_5_xrp_tight(surface: "FullDataSurface") -> StrategyDecision:
    p_xrp = getattr(surface, "probability_lgb_v9_5_xrp", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_xrp is None:
        return _skip(
            "v9_5_xrp_model_not_loaded",
            {"probability_lgb_v9_5_xrp": None},
        )

    p_xrp = float(p_xrp)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_xrp": p_xrp,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_xrp >= up_threshold:
        direction = "UP"
    elif p_xrp <= down_threshold:
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
        return _skip("awaiting_consec_ticks", meta)

    confidence_score = float(abs(p_xrp - 0.5) * 2.0)
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
        entry_reason="v9_5_xrp_tight_pass",
        skip_reason=None,
        metadata=meta,
    )
