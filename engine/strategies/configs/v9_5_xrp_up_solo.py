"""v9_5_xrp_up_solo — UP-only XRP 5m strategy on probability_lgb_v9_5_xrp_pure (GHOST).

Fires ONLY on the UP side reading probability_lgb_v9_5_xrp_pure — the PURE
(un-blended) column for the v9.5 XRP-trained model. Switched from BLEND to
PURE 2026-05-26 (timesfm commit e1ba39d, V9_5_XRP_PURE_ENABLED=true, column
signal_evaluations.probability_lgb_v9_5_xrp_pure migration applied). This
strategy:
  1. Is UP-only (no DOWN side — XRP DOWN shows no signal today, needs research)
  2. Uses the tighter eval_offset [60, 180] vs v9_5_xrp_blend's [60, 240]

Asset: XRP only. Strategy will SKIP defensively on any non-XRP surface.

WHY UP-ONLY?
  Today's live signal evaluation sweep (RDS note #694, 2026-05-25) shows
  XRP DOWN at available thresholds has no stable signal; this needs further
  research. The UP pocket shows genuine edge (85-90% WR today). Scaffolding
  the DOWN side now would add noise. If DOWN develops a signal, Billy can
  create a companion v9_5_xrp_down_solo.

RELATION TO v9_5_xrp_late_band_AB_blend:
  v9_5_xrp_late_band_AB_blend was intended to provide XRP UP coverage via
  a late-band composite gate. Today's sweep (RDS note #694) shows the TIGHT
  raw threshold approach (p >= 0.82, eval_offset [60, 180]) achieves the same
  or better WR with simpler logic. This strategy effectively replaces the
  functional goal of v9_5_xrp_late_band_AB_blend at the operating threshold
  directly, without composite gate complexity.

Operating point (RDS note #694, today's live sweep, 2026-05-25):
  - UP   when probability_lgb_v9_5_xrp_pure >= 0.90
           (PURE threshold; BLEND threshold was 0.82 — PURE distributes
            differently so threshold is set per Billy's review)
  - NO DOWN side (XRP DOWN shows no signal today — needs research)
  - eval_offset in [60, 180]  — tighter than v9_5_xrp_blend's [60, 240];
    drops the weak Δ=240s tail
  - min_consecutive_pass_ticks=1

Direction-aware fill-band gate (RDS note #664, 2026-05-25):
  - UP  (YES): skip if fill < entry_floor_up (default 0.60)
  - DOWN (NO): entry_cap_down present for YAML parity but UNUSED — no DOWN fires.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for shadow window.

References:
  RDS note #694 — exhaustive threshold sweep (2026-05-25), today's findings
  RDS note #664 — direction-aware fill-band gate spec
  data_surface.py line 371 — probability_lgb_v9_5_xrp definition
  Sibling strategies: v9_5_xrp_blend (UP p>=0.82 / DOWN p<=0.20, eval [60,240])
                      v9_5_xrp_tight_blend (UP p>=0.95 / DOWN p<=0.05)
                      v9_5_xrp_late_band_AB_blend (composite gate approach)
  Engine precedent: v9_2_eth_solo.py (PR #604)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_xrp_up_solo"
_VERSION = "1.0.0"

# Operating point — switched to PURE column 2026-05-26 (timesfm commit e1ba39d).
# PURE threshold: p >= 0.90 (BLEND was 0.82; PURE distributes differently).
# NO down threshold — DOWN side shows no signal today (needs research).
_DEFAULT_UP_THRESHOLD = 0.90

# Best eval-offset band for XRP UP today (2026-05-25).
# Tighter than v9_5_xrp_blend's [60, 240] — drops the weak Δ=240s tail.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Fill-band gate defaults (RDS note #664).
# entry_floor_up: block YES fills below 0.60 (ghost markets).
# entry_cap_down: present for YAML parity but UNUSED — no DOWN fires here.
_DEFAULT_ENTRY_FLOOR_UP = 0.60    # skip UP/YES fires if fill < 0.60 (RDS #664)
_DEFAULT_ENTRY_CAP_DOWN = 0.90    # UNUSED — no DOWN fires in this strategy
_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "XRP"

# Consecutive-tick state. Module-local so this strategy's state cannot
# collide with sibling strategies (v9_5_xrp_blend, v9_5_xrp_tight_blend,
# v9_5_xrp_late_band_AB_blend).
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


def evaluate_v9_5_xrp_up_solo(surface: "FullDataSurface") -> StrategyDecision:
    # 2026-05-26: switched from BLEND (probability_lgb_v9_5_xrp) to PURE
    # (probability_lgb_v9_5_xrp_pure) — timesfm commit e1ba39d,
    # V9_5_XRP_PURE_ENABLED=true. Threshold adjusted to 0.90 for PURE.
    p_xrp = getattr(surface, "probability_lgb_v9_5_xrp_pure", None)
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
            "v9_5_xrp_pure_not_available",
            {"probability_lgb_v9_5_xrp_pure": None},
        )

    p_xrp = float(p_xrp)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_xrp_pure": p_xrp,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # UP-only strategy. DOWN side shows no stable signal today (RDS #694).
    if p_xrp >= up_threshold:
        direction = "UP"
    else:
        return _skip("conviction_below_threshold", meta)

    meta["direction"] = direction

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_2_eth_solo.
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
    # UP (YES): skip if fill < entry_floor_up (default 0.60).
    # DOWN (NO): entry_cap_down is present in YAML for parity but UNUSED here
    #            (there are no DOWN fires in this UP-only strategy).
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        entry_floor_up = float(_gp.get_float("entry_floor_up", None, _DEFAULT_ENTRY_FLOOR_UP))
        meta["fill_price"] = fill_price
        meta["entry_floor_up"] = entry_floor_up
        if direction in ("UP", "YES") and fill_price < entry_floor_up:
            return _skip(
                f"fill_below_up_floor:{fill_price:.3f}<{entry_floor_up:.3f}",
                meta,
            )

    confidence_score = float(abs(p_xrp - 0.5) * 2.0)
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
        entry_reason="v9_5_xrp_up_solo_pass",
        skip_reason=None,
        metadata=meta,
    )
