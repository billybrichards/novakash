"""v9_2_eth_solo — bidirectional ETH 5m strategy on raw probability_lgb_v9_2_eth (GHOST).

Fires on raw probability_lgb_v9_2_eth — a PURE LGB output with no HF classifier
blend (contrast with probability_lgb_v9_5_eth which IS blended). Because there is
no classifier saturation ceiling, the model can reach the full 0-1 range and the
0.80/0.45 thresholds are well-supported.

Asset: ETH only. Strategy will SKIP defensively on any non-ETH surface.

Operating point (Billy approved, 2026-05-25, per RDS note #665 request):
  - UP   when probability_lgb_v9_2_eth >= 0.80
           -> 7d tick-level: 83.9% WR on n=5825 (eval_offset 60-180)
  - DOWN when probability_lgb_v9_2_eth <= 0.45
           -> 7d tick-level: 82.9% WR on n=5190 (eval_offset 60-180)
  - eval_offset in [60, 180]  — best band from 5-way sweep (2026-05-25)
  - min_consecutive_pass_ticks=1

CAUTION — DOWN side:
  RDS note #665 reported DOWN at n=13 / 100% WR (short lookback). The 7d
  dataset gives n=470 windows at 69.4% WR at the window level (vs 82.9% at
  tick level). The direction-aware fill gate (entry_cap_down=0.90) is active.
  Billy should review DOWN performance after 50+ GHOST fires; consider
  disabling DOWN if window WR < 80%.

Direction-aware fill-band gate (RDS note #664, 2026-05-25):
  - UP  (YES): skip if fill < entry_floor_up  (default 0.60)
  - DOWN (NO): skip if fill >= entry_cap_down (default 0.90)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for shadow window.

Sister strategies:
  - v9_2_eth_late_band_AB_blend: UP-only, eval_offset [60,90], p >= 0.82
  - v9_2_eth_raw_lgb:            both dirs, eval_offset [120,210], p >= 0.96 / p <= 0.04

References:
  RDS note #665 — broad sweep finding (source of Billy's request)
  RDS note #666 — this strategy's design rationale + pure/blend verdict
  data_surface.py lines 246-256 — probability_lgb_v9_2_eth definition
  Engine precedent: v9_2_eth_late_band_AB_blend.py (PR #582)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_eth_solo"
_VERSION = "1.0.0"

# Operating point per Billy's request + RDS note #665 / 2026-05-25 sweep.
_DEFAULT_UP_THRESHOLD = 0.80
_DEFAULT_DOWN_THRESHOLD = 0.45

# Best eval-offset band from 5-way sweep (2026-05-25):
#   [60,180] → UP 83.9% / DOWN 82.9% tick-level WR (n=5825 / 5190)
#   [60,240] drops DOWN to 78.8% with lower quality late ticks.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Fill-band gate defaults (RDS note #664).
# Permissive defaults — strategies that don't set these in YAML are unaffected.
_DEFAULT_ENTRY_FLOOR_UP = 0.60    # block YES fills below 0.60 (ghost markets)
_DEFAULT_ENTRY_CAP_DOWN = 0.90    # block NO fills at 0.90+ (near-resolved YES)
_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Consecutive-tick state. Module-local so this strategy's state cannot
# collide with sibling strategies (v9_2_eth_late_band_AB_blend, v9_2_eth_raw_lgb).
# Maps (window_ts, direction) -> (count, last_seen_ts).
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


def evaluate_v9_2_eth_solo(surface: "FullDataSurface") -> StrategyDecision:
    p_eth = getattr(surface, "probability_lgb_v9_2_eth", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=ETH but
    # belt-and-braces — refuse to fire if the registry ever wires a non-ETH
    # surface in. The v9.2 ETH model is not calibrated for other assets.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_eth is None:
        return _skip(
            "v9_2_eth_model_not_loaded",
            {"probability_lgb_v9_2_eth": None},
        )

    p_eth = float(p_eth)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_2_eth": p_eth,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_eth >= up_threshold:
        direction = "UP"
    elif p_eth <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    meta["direction"] = direction

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_2_eth_raw_lgb.
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
    # UP (YES): skip if fill < entry_floor_up (default 0.60 for v9_2_eth_solo).
    # DOWN (NO): skip if fill >= entry_cap_down (default 0.90 for v9_2_eth_solo).
    # Defaults are from YAML but fall back to _DEFAULT values if not set.
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        entry_floor_up = float(_gp.get_float("entry_floor_up", None, _DEFAULT_ENTRY_FLOOR_UP))
        entry_cap_down = float(_gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN))
        meta["fill_price"] = fill_price
        meta["entry_floor_up"] = entry_floor_up
        meta["entry_cap_down"] = entry_cap_down
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
        entry_reason="v9_2_eth_solo_pass",
        skip_reason=None,
        metadata=meta,
    )
