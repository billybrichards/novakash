"""v9_5_xrp_blend — XRP 5m drop-in strategy reading v9.5 XRP LGB (GHOST).

Mirrors v9_3_btc_raw_lgb but fires on the NEW probability_lgb_v9_5_xrp signal
(timesfm PR #160, merged 2026-05-23). No v9_ensemble base-gate delegation.
No cohort gates, no sister-pair veto, no cell pauses, no blocked hours.
Co-exists with the sibling v9_5_xrp_tight strategy in this PR — both read
the SAME probability_lgb_v9_5_xrp column but at DIFFERENT operating points.

NOTE (2026-05-23): The earlier v9_2_xrp_raw_lgb strategy was never wired
into production (no strategy_configs row, no yaml/py file, no writer for
its column). v9.5 XRP is the canonical XRP path going forward. The
signal_evaluations.probability_lgb_v9_2_xrp column remains in the DB
(harmless, all-NULL) but no engine code references it.

Asset: XRP only. Strategy will SKIP on any non-XRP surface (defensive guard
— the v9.5 XRP model is calibrated for XRP only; firing on BTC/ETH would
be undefined behaviour).

Criteria (timesfm PR #160 + /tmp/v9_5_xrp_strategy_design.md walk-forward CV):
  - UP   when probability_lgb_v9_5_xrp >= 0.82 -> 74.8% WR on 476 windows
  - DOWN when probability_lgb_v9_5_xrp <= 0.20 -> 71.9% WR on 551 windows
  - eval_offset in [60, 240]  (XRP fires concentrate mid-to-late window —
    median first-fire stc 154s UP / 170s DOWN)
  - min_consecutive_pass_ticks=1 (matches v9_3_btc_raw_lgb)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window; raise once shadow data validates the thresholds.

Timesfm-repo PR #160 — v9.5 XRP emission + production training script.
Strategy design doc: /tmp/v9_5_xrp_strategy_design.md
Engine precedent: v9_3_btc_raw_lgb (commit 5e8c147).
Sibling strategy in this PR: v9_5_xrp_tight (high-precision corner at 0.95/0.05).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_xrp_blend"
_VERSION = "1.0.0"

# Walk-forward CV operating point per timesfm PR #160 + strategy_design.md.
_DEFAULT_UP_THRESHOLD = 0.82
_DEFAULT_DOWN_THRESHOLD = 0.20

# XRP fires concentrate mid-to-late window (first-fire stc median 154s UP /
# 170s DOWN — eval_offset 130-146s). Widen to 60-240 to cover the q25-q75
# fire band with margin.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 240

_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
# 1-tick consecutive gate matches v9_3_btc_raw_lgb.
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "XRP"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Resets when direction changes or the gap between evals exceeds _MAX_GAP_S.
# Module-local so this strategy's consecutive-tick state cannot collide with
# the sibling v9_5_xrp_tight strategy.
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


def evaluate_v9_5_xrp_blend(surface: "FullDataSurface") -> StrategyDecision:
    p_xrp = getattr(surface, "probability_lgb_v9_5_xrp", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=XRP but
    # belt-and-braces — refuse to fire if the registry ever wires a non-XRP
    # surface in. The v9.5 XRP model is not calibrated for other assets.
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

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_3_btc_raw_lgb.
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

    # Direction-aware fill-band gate (RDS note #664, 2026-05-25).
    # UP (YES): skip if fill < entry_floor_up (default 0.0 = permissive).
    # DOWN (NO): skip if fill >= entry_cap_down (default 1.0 = permissive).
    # Defaults are permissive so strats NOT setting these fields are unaffected.
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
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
        entry_reason="v9_5_xrp_blend_pass",
        skip_reason=None,
        metadata=meta,
    )
