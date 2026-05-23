"""v9_3_btc_tight — high-precision BTC 5m strategy reading v9.3 BTC LGB (GHOST).

Sibling of v9_3_btc_raw_lgb. Reads the SAME probability_lgb_v9_3_btc signal
but at the tight-corner operating point (UP p>=0.935 / DOWN p<=0.065) and
constrained to the early-window band (eval_offset in [20, 170], i.e.
stc in [130, 280]).

No v9_ensemble base-gate delegation. No cohort gates, no sister-pair veto,
no cell pauses, no blocked hours. Co-exists with v9_3_btc_raw_lgb — both
read the same probability field but have independent consecutive-tick state
and DIFFERENT operating points.

Asset: BTC only. Strategy will SKIP on any non-BTC surface (defensive guard).

Criteria (timesfm notes #585 / #587 / #589 / #590 walk-forward CV):
  - UP   when probability_lgb_v9_3_btc >= 0.935 -> 90.3% WR on 1725 windows
  - DOWN when probability_lgb_v9_3_btc <= 0.065 -> 90.4% WR on 1590 windows
  - eval_offset in [20, 170] (early-window subsegment)
  - min_consecutive_pass_ticks=1

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window.

Timesfm-repo notes #585 / #587 / #589 / #590 — walk-forward CV results.
BTC ebook chapter: /home/billyrichards/scans2025/v9_3_btc_ebook_chapter.html
Sibling strategy in this PR: v9_3_btc_raw_lgb (drop-in replacement).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_3_btc_tight"
_VERSION = "1.0.0"

# Walk-forward CV tight-corner operating point.
_DEFAULT_UP_THRESHOLD = 0.935
_DEFAULT_DOWN_THRESHOLD = 0.065

# Early-window band — stc in [130, 280] -> eval_offset in [20, 170]
# (window length 300s; eval_offset = 300 - seconds_to_close).
_DEFAULT_EVAL_OFFSET_MIN = 20
_DEFAULT_EVAL_OFFSET_MAX = 170

_DEFAULT_ENTRY_CAP = 0.935
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.935
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "BTC"

# Module-local state — independent from v9_3_btc_raw_lgb's state even
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


def evaluate_v9_3_btc_tight(surface: "FullDataSurface") -> StrategyDecision:
    p_btc = getattr(surface, "probability_lgb_v9_3_btc", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_btc is None:
        return _skip(
            "v9_3_btc_model_not_loaded",
            {"probability_lgb_v9_3_btc": None},
        )

    p_btc = float(p_btc)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_3_btc": p_btc,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_btc >= up_threshold:
        direction = "UP"
    elif p_btc <= down_threshold:
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

    confidence_score = float(abs(p_btc - 0.5) * 2.0)
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
        entry_reason="v9_3_btc_tight_pass",
        skip_reason=None,
        metadata=meta,
    )
