"""v9_2_v12_combo_pure — v9.2 + v12 BTC PURE AND-combo strategy (GHOST).

Sibling of v9_2_v12_combo (LIVE on the BLENDED probability columns). This
variant explicitly reads the NEW PURE columns:
  probability_lgb_v9_2_pure  AND  probability_lgb_v12_pure
(added in this PR + sibling timesfm PR #163). The blended columns
(probability_lgb_v9_2 / probability_lgb_v12) stay untouched and the LIVE
v9_2_v12_combo strategy continues to read them.

Per BTC walk-forward agent's finding (/tmp/btc_walkforward_results.md):
the LIVE v9_2_v12_combo strategy validates much better on PURE values
(281 fires @ 96.4% WR) than on BLEND values (4 fires @ 100% WR). The
281 actual fires on PURE matches the LIVE strategy's ~13 fires/day x
21 days. This explicit PURE strategy lets us A/B test the question
unambiguously — if LIVE IS already firing on PURE, this strategy will
mirror its fire log byte-for-byte.

Criteria (walk-forward CV at /tmp/btc_walkforward_results.md):
  - UP   when probability_lgb_v9_2_pure >= 0.75 AND probability_lgb_v12_pure >= 0.70
    -> 96.4% WR on 281 windows (Wilson LB 93.6%), ~13.4 fires/day
  - DOWN when probability_lgb_v9_2_pure <= 0.29 AND probability_lgb_v12_pure <= 0.27
    -> 95.1% WR on 412 windows (Wilson LB 92.6%), ~19.6 fires/day
  - eval_offset in [60, 210]  (matches existing v9_2_v12_combo LIVE band)
  - One qualifying tick is enough (matches LIVE combo)

Adding the symmetric DOWN side roughly doubles the daily fire rate at
similar WR vs the current LIVE blend strategy (UP only).

Asset: BTC only. Strategy will SKIP defensively on any non-BTC surface.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually per
feedback_no_auto_promote.md. $5 max_position_usd for the initial shadow
window; raise after the A/B vs LIVE finishes.

Sibling timesfm PR: #163 (feat/v9_3_btc_pure_lgb_emission).
Sibling engine strategy: v9_3_btc_pure_lgb.
Engine precedent: feat/v9_5_eth_pure_lgb_strategy (ETH variant).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_v12_combo_pure"
_VERSION = "1.0.0"

# Walk-forward CV operating point per /tmp/btc_walkforward_results.md.
# UP thresholds match existing LIVE v9_2_v12_combo (0.75, 0.70).
# DOWN thresholds are the new symmetric DOWN side per walk-forward
# Phase 3 (95%+ WR target).
_DEFAULT_UP_V92_THRESHOLD = 0.75
_DEFAULT_UP_V12_THRESHOLD = 0.70
_DEFAULT_DOWN_V92_THRESHOLD = 0.29
_DEFAULT_DOWN_V12_THRESHOLD = 0.27

# eval_offset band — matches existing v9_2_v12_combo LIVE [60, 210].
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210

# Higher entry/gtc caps than the LIVE blend sibling — PURE columns reach
# 0.956 (v9.2) / 0.921 (v12) vs blend ceilings 0.826 / 0.805. Headroom
# for the higher-conviction tail.
_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96

# 1-tick consecutive gate matches LIVE v9_2_v12_combo.
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Module-local so it cannot collide with the LIVE
# v9_2_v12_combo or v9_3_btc_pure_lgb sibling.
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


def evaluate_v9_2_v12_combo_pure(surface: "FullDataSurface") -> StrategyDecision:
    p_v92_pure = getattr(surface, "probability_lgb_v9_2_pure", None)
    p_v12_pure = getattr(surface, "probability_lgb_v12_pure", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_v92_pure is None:
        return _skip(
            "v9_2_pure_model_not_loaded",
            {"probability_lgb_v9_2_pure": None},
        )
    if p_v12_pure is None:
        return _skip(
            "v12_pure_model_not_loaded",
            {"probability_lgb_v12_pure": None},
        )

    p_v92_pure = float(p_v92_pure)
    p_v12_pure = float(p_v12_pure)

    up_v92 = _gp.get_float("up_v92_threshold", None, _DEFAULT_UP_V92_THRESHOLD)
    up_v12 = _gp.get_float("up_v12_threshold", None, _DEFAULT_UP_V12_THRESHOLD)
    down_v92 = _gp.get_float("down_v92_threshold", None, _DEFAULT_DOWN_V92_THRESHOLD)
    down_v12 = _gp.get_float("down_v12_threshold", None, _DEFAULT_DOWN_V12_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_2_pure": p_v92_pure,
        "probability_lgb_v12_pure": p_v12_pure,
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

    if p_v92_pure >= up_v92 and p_v12_pure >= up_v12:
        direction = "UP"
    elif p_v92_pure <= down_v92 and p_v12_pure <= down_v12:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # N-consecutive-tick confirmation gate (runtime-tunable).
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

    # Direction-aware fill-band gate (RDS note #664, 2026-05-25).
    # UP (YES): skip if fill < entry_floor_up (default 0.0 = permissive).
    # DOWN (NO): skip if fill >= entry_cap_down (default 1.0 = permissive).
    # Defaults are permissive so strats NOT setting these fields are unaffected.
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        # fill_price sourced from clob_implied_up when surface has it.
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        entry_floor_up = float(_gp.get_float("entry_floor_up", None, 0.0))
        entry_cap_down = float(_gp.get_float("entry_cap_down", None, 1.0))
        _efd_raw = _gp._lookup("entry_floor_down", None, None)
        entry_floor_down = float(_efd_raw) if _efd_raw is not None else None
        # fill_price = YES/UP leg; NO leg proxy = 1 - fill_price.
        down_fill_proxy = 1.0 - fill_price
        meta["fill_price"] = fill_price
        meta["entry_floor_up"] = entry_floor_up
        meta["entry_cap_down"] = entry_cap_down
        meta["entry_floor_down"] = entry_floor_down
        if direction in ("UP", "YES") and fill_price < entry_floor_up:
            return _skip(
                f"fill_below_up_floor:{fill_price:.3f}<{entry_floor_up:.3f}",
                meta,
            )
        if direction in ("DOWN", "NO") and fill_price >= entry_cap_down:
            return _skip(
                f"fill_above_down_cap:{fill_price:.3f}>={entry_cap_down:.3f}",
                meta,
            )
        if (
            direction in ("DOWN", "NO")
            and entry_floor_down is not None
            and down_fill_proxy < entry_floor_down
        ):
            return _skip(
                f"entry_floor_down ({down_fill_proxy:.3f} < {entry_floor_down})",
                meta,
            )

    # Use the stronger signal for confidence scoring (matches LIVE combo).
    confidence_score = float(
        max(abs(p_v92_pure - 0.5), abs(p_v12_pure - 0.5)) * 2.0
    )
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
        entry_reason="v9_2_v12_combo_pure_pass",
        skip_reason=None,
        metadata=meta,
    )
