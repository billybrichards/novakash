"""v9_3_btc_blend — drop-in replacement for v9_2_raw_lgb (BTC 5m, GHOST).

Mirrors v9_2_raw_lgb but fires on the NEW probability_lgb_v9_3_btc signal
(companion timesfm PR not yet opened; Billy approves before that's created).
No v9_ensemble base-gate delegation. No cohort gates, no sister-pair veto,
no cell pauses, no blocked hours. Co-exists with v9_2_raw_lgb — both read
different probability fields and have independent consecutive-tick state.

Asset: BTC only. Strategy will SKIP on any non-BTC surface (defensive guard
— the v9.3 BTC model is calibrated for BTC only; firing on ETH/XRP would
be undefined behaviour).

Criteria (timesfm notes #585 / #587 / #589 / #590 walk-forward CV):
  - UP   when probability_lgb_v9_3_btc >= 0.72  -> 79.9% WR on 2419 windows
  - DOWN when probability_lgb_v9_3_btc <= 0.20  -> 82.8% WR on 2320 windows
  - eval_offset in [60, 210] (matches v9_2_raw_lgb band)
  - min_consecutive_pass_ticks=1 (matches v9_2_raw_lgb so shadow-vs-LIVE is
    a like-for-like comparison)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window; raise once shadow data validates the thresholds.

Timesfm-repo notes #585 / #587 / #589 / #590 — walk-forward CV results.
BTC ebook chapter: /home/billyrichards/scans2025/v9_3_btc_ebook_chapter.html
Engine precedent: feat/v9_5_eth_strategy (PR pending).
Sibling strategy in this PR: v9_3_btc_tight (high-precision corner).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_3_btc_blend"
_VERSION = "1.0.0"

# Walk-forward CV operating point per timesfm notes #585/#587/#589/#590.
_DEFAULT_UP_THRESHOLD = 0.72
_DEFAULT_DOWN_THRESHOLD = 0.20

# Matches v9_2_raw_lgb band so the shadow-vs-LIVE comparison is like-for-like.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210

_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
# 1-tick consecutive gate matches v9_2_raw_lgb — keep selectivity profile
# identical so the shadow-vs-LIVE delta is purely the model swap.
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Resets when direction changes or the gap between evals exceeds _MAX_GAP_S.
# Module-local so this strategy's consecutive-tick state cannot collide with
# the sibling v9_3_btc_tight strategy or with v9_2_raw_lgb.
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


def evaluate_v9_3_btc_blend(surface: "FullDataSurface") -> StrategyDecision:
    p_btc = getattr(surface, "probability_lgb_v9_3_btc", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=BTC but
    # belt-and-braces — refuse to fire if the registry ever wires a non-BTC
    # surface in. The v9.3 BTC model is not calibrated for other assets.
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

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_2_raw_lgb.
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
        entry_reason="v9_3_btc_blend_pass",
        skip_reason=None,
        metadata=meta,
    )
