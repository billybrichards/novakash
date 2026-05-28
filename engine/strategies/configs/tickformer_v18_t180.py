"""tickformer_v18_t180 — TickFormer v18 balanced t-180 SHADOW strategy.

Reads `probability_tickformer_v18` (NUMERIC) emitted by timesfm-service
(sister PR on the magic-model repo). v18 is the balanced successor to v17
— same hybrid transformer + multi-step autoregressive head but trained
across the full t-60..t-220 window with a uniform precision loss. It is
the *recommended* model for t-180 entries: ~16 trades/day at 92% WR
combined UP+DN.

Headline pocket from the v18 val sweep:

    thr 0.90, eval_offset_remaining 60s   -> ~96% WR
    thr 0.90, eval_offset_remaining 180s  -> ~92% WR (recommended)
    thr 0.90, eval_offset_remaining 220s  -> ~85% WR

The defining v18 property is that WR holds 85%+ across the *entire*
eval_offset_remaining band — there is no sharp late-window cliff like v16.
This makes it the safest broad-band choice for production once shadow data
validates.

This strategy is registered SHADOW by default — Billy promotes manually
per feedback_no_auto_promote.md. A `shadow_only: bool` gate_param flag
forces SKIP-with-decision-record behaviour even if the YAML mode is
mis-set; it must be flipped to false in YAML before any real fire emits.

DOWN side is symmetric: `down_threshold = 1 - up_threshold`. The
tickformer_trade_signal cross-check is shared across v16/v17/v18 — only
an explicit *opposite* signal blocks the fire.

Sibling timesfm PR: magic-model branch (probability_tickformer_v18
emission on /v4/snapshot.timescales.5m).
Engine precedent for SHADOW pattern: v9_5_xrp_up_solo.py / tickformer_v16_pure.py.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "tickformer_v18_t180"
_VERSION = "1.0.0"

# Default operating point — recommended balanced tier (thr 0.90, broad band).
# v18 sustains 85%+ WR across the full t-60..t-220 band at thr 0.90; this
# is the production-recommended default per the v18 spec.
_DEFAULT_UP_THRESHOLD = 0.90
_DEFAULT_DOWN_THRESHOLD = 0.10  # 1 - up_threshold, symmetric

# Broad band — v18's defining property. Operator narrows in YAML for
# tighter WR (60-120 for ~94-96% WR pocket).
_DEFAULT_EVAL_OFFSET_REMAINING_MIN = 60
_DEFAULT_EVAL_OFFSET_REMAINING_MAX = 220

# Standard cap/sizing defaults (mirror v16/v17 for like-for-like comparison).
_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_ENTRY_FLOOR_UP = 0.60
_DEFAULT_ENTRY_CAP_DOWN = 0.90
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1

# SHADOW gate default — when truthy, the strategy returns SKIP with a
# decision-record metadata payload but never emits a TRADE action.
_DEFAULT_SHADOW_ONLY = True

# Consecutive-tick state. Module-local — keep isolated from v16/v17.
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


def _eval_offset_remaining(surface: "FullDataSurface") -> int | None:
    """Return seconds-remaining in the 5m window."""
    explicit = getattr(surface, "eval_offset_remaining", None)
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            return None
    eval_offset = getattr(surface, "eval_offset", None)
    if eval_offset is None:
        return None
    try:
        return max(0, 300 - int(eval_offset))
    except (TypeError, ValueError):
        return None


def evaluate_tickformer_v18_t180(surface: "FullDataSurface") -> StrategyDecision:
    p = getattr(surface, "probability_tickformer_v18", None)
    trade_signal = getattr(surface, "tickformer_trade_signal", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    if p is None:
        return _skip(
            "tickformer_v18_not_available",
            {"probability_tickformer_v18": None, "asset": asset},
        )
    p = float(p)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float(
        "down_threshold", None, max(0.0, min(1.0, 1.0 - up_threshold))
    )
    rem_min = _gp.get_int(
        "eval_offset_remaining_min", None, _DEFAULT_EVAL_OFFSET_REMAINING_MIN
    )
    rem_max = _gp.get_int(
        "eval_offset_remaining_max", None, _DEFAULT_EVAL_OFFSET_REMAINING_MAX
    )
    shadow_only = bool(_gp.get_int("shadow_only", None, int(_DEFAULT_SHADOW_ONLY)))

    remaining = _eval_offset_remaining(surface)

    meta = {
        "probability_tickformer_v18": p,
        "tickformer_trade_signal": trade_signal,
        "eval_offset": eval_offset,
        "eval_offset_remaining": remaining,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_remaining_min": rem_min,
        "eval_offset_remaining_max": rem_max,
        "asset": asset,
        "shadow_only": shadow_only,
    }

    if remaining is None or remaining < rem_min or remaining > rem_max:
        return _skip("outside_eval_offset_remaining_band", meta)

    if p >= up_threshold:
        direction = "UP"
    elif p <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # Trade-signal cross-check: only an explicit opposite label blocks.
    if trade_signal in ("UP", "DOWN"):
        if direction != trade_signal:
            meta["mismatch_trade_signal"] = trade_signal
            return _skip("trade_signal_disagrees", meta)

    meta["direction"] = direction

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

    # Direction-aware fill-band gate (RDS note #664).
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        try:
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            fill_price = None
    if fill_price is not None:
        entry_floor_up = float(
            _gp.get_float("entry_floor_up", None, _DEFAULT_ENTRY_FLOOR_UP)
        )
        entry_cap_down = float(
            _gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN)
        )
        meta["fill_price"] = fill_price
        meta["entry_floor_up"] = entry_floor_up
        meta["entry_cap_down"] = entry_cap_down
        if direction == "UP" and fill_price < entry_floor_up:
            return _skip(
                f"fill_below_up_floor:{fill_price:.3f}<{entry_floor_up:.3f}",
                meta,
            )
        if direction == "DOWN" and fill_price > entry_cap_down:
            return _skip(
                f"fill_above_down_cap:{fill_price:.3f}>{entry_cap_down:.3f}",
                meta,
            )

    confidence_score = float(abs(p - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float("collateral_pct", None, _DEFAULT_COLLATERAL_PCT)
    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct
    meta["gtc_cap"] = gtc_cap
    meta["strategy_id"] = _STRATEGY_ID
    meta["strategy_version"] = _VERSION

    # SHADOW kill switch — emit decision record but NEVER trade live.
    if shadow_only:
        meta["would_trade"] = True
        meta["would_direction"] = direction
        meta["would_confidence_score"] = confidence_score
        return _skip("shadow_only_no_trade", meta)

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
        entry_reason="tickformer_v18_t180_pass",
        skip_reason=None,
        metadata=meta,
    )
