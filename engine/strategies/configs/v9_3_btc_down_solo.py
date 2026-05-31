"""v9_3_btc_down_solo — DOWN-only BTC 5m strategy on probability_lgb_v9_3_btc_pure (GHOST).

v2.0.0 — COLUMN SWITCH: probability_lgb_v9_3_btc (BLEND) → probability_lgb_v9_3_btc_pure (PURE)
         THRESHOLD TIGHTENED: p <= 0.50 → p <= 0.20 (= DOWN @ 0.80 confidence per v9.3.1 OOF)

Fires ONLY on the DOWN side reading probability_lgb_v9_3_btc_pure — the PURE LGB+iso
output BEFORE the blend_ensemble step in app/v2_scorer.py (data_surface.py line 349).

MIGRATION from v1.0.0 (BLEND-based):
  v1.0.0 read probability_lgb_v9_3_btc at p <= 0.50 (83.3% WR n=18 — single day,
          limited reliability due to small n).
  v2.0.0 reads probability_lgb_v9_3_btc_pure at p <= 0.20 (87.0% WR n=130K OOF,
          v9.3.1 overnight retrain, 468K total OOF records, 2026-05-25).
  The PURE column is not subject to TimesFM HF classifier saturation (~0.916 blend cap);
  it occupies the full 0-1 probability range. The DOWN @ p <= 0.20 PURE pocket is
  equivalent to DOWN @ 0.80 confidence, which is the primary threshold from the OOF audit.

Asset: BTC only. Strategy will SKIP defensively on any non-BTC surface.

WHY DOWN-ONLY?
  The UP side is covered by companion strategy v9_3_btc_pure_up_solo (p >= 0.85,
  94.3% WR n=90K per v9.3.1 OOF). Both read probability_lgb_v9_3_btc_pure but in
  opposite directions with no threshold overlap (DOWN p<=0.20; UP p>=0.85) — no
  market conflict.

Operating point (v9.3.1 OOF retrain, 468K records, 2026-05-25):
  - DOWN when probability_lgb_v9_3_btc_pure <= 0.20
           -> 87.0% WR n=130K (DOWN @ 0.80 confidence)
           -> 92.7% WR n=83K  (DOWN @ 0.85 confidence, p<=0.15 override)
           -> 94.2% WR n=63K  (DOWN @ 0.90 confidence, p<=0.10 override)
  - NO UP side — covered by v9_3_btc_pure_up_solo
  - eval_offset in [60, 180]  — best band consistent with v9_2_eth_solo sweep
  - min_consecutive_pass_ticks=2 (conservative for PURE column migration)

Direction-aware fill-band gate (RDS note #664, 2026-05-25):
  - DOWN (NO): skip if YES fill >= entry_cap_down (default 0.90)
  - UP  (YES): entry_floor_up present in YAML for parity but UNUSED here
               (there are no UP fires in this DOWN-only strategy).

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for shadow window.

References:
  v9.3.1 OOF retrain (2026-05-25) — 468K records, DOWN p<=0.20 = 87.0% WR n=130K
  data_surface.py line 349 — probability_lgb_v9_3_btc_pure definition
  RDS note #664 — direction-aware fill-band gate spec
  Sibling strategy: v9_3_btc_pure_up_solo (UP p>=0.85, 94.3% WR n=90K)
  v9_3_btc_pure_lgb: UP p>=0.935 / DOWN p<=0.065 (also reads PURE column)
  Engine precedent: v9_2_eth_solo.py (PR #604)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_3_btc_down_solo"
_VERSION = "2.0.0"

# v9.3.1 OOF retrain operating point (468K records, 2026-05-25).
# DOWN p <= 0.20 = 87.0% WR n=130K (DOWN @ 0.80 confidence).
# Tighten via runtime_overrides: p<=0.15 = 92.7% WR n=83K, p<=0.10 = 94.2% WR n=63K.
# NO up threshold — UP side covered by v9_3_btc_pure_up_solo.
_DEFAULT_DOWN_THRESHOLD = 0.20

# Best eval-offset band from 5-way sweep (consistent with v9_2_eth_solo,
# 2026-05-25): [60, 180] avoids the weak Δ=240s tail.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Fill-band gate defaults (RDS note #664).
# entry_floor_up: present for YAML parity but UNUSED — no UP fires here.
# entry_cap_down: block NO fills at 0.90+ (near-resolved YES).
_DEFAULT_ENTRY_FLOOR_UP = 0.65    # UNUSED — no UP fires in this strategy
_DEFAULT_ENTRY_CAP_DOWN = 0.90    # block NO fills at 0.90+ (near-resolved YES)
_DEFAULT_ENTRY_CAP = 0.90
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 2     # conservative for PURE column migration
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Module-local so this strategy's state cannot
# collide with sibling strategies (v9_3_btc_pure_lgb, v9_3_btc_blend,
# v9_3_btc_tight_blend, v9_3_btc_pure_up_solo).
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


def evaluate_v9_3_btc_down_solo(surface: "FullDataSurface") -> StrategyDecision:
    # v2.0.0: reads probability_lgb_v9_3_btc_pure (PURE column, data_surface.py line 349).
    # v1.0.0 read probability_lgb_v9_3_btc (BLEND column) — migrated 2026-05-25.
    p_btc_pure = getattr(surface, "probability_lgb_v9_3_btc_pure", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=BTC but
    # belt-and-braces — refuse to fire if the registry ever wires a non-BTC
    # surface in. The v9.3 BTC PURE model is not calibrated for other assets.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_btc_pure is None:
        return _skip(
            "v9_3_btc_pure_model_not_loaded",
            {"probability_lgb_v9_3_btc_pure": None},
        )

    p_btc_pure = float(p_btc_pure)

    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_3_btc_pure": p_btc_pure,
        "eval_offset": eval_offset,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # DOWN-only strategy. UP side covered by v9_3_btc_pure_up_solo (p>=0.85).
    if p_btc_pure <= down_threshold:
        direction = "DOWN"
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
    # DOWN (NO): skip if YES fill >= entry_cap_down (default 0.90).
    # UP  (YES): entry_floor_up is present in YAML for parity but UNUSED here
    #            (there are no UP fires in this DOWN-only strategy).
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        entry_cap_down = float(_gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN))
        _efd_raw = _gp._lookup("entry_floor_down", None, None)
        entry_floor_down = float(_efd_raw) if _efd_raw is not None else None
        # For this file fill_price = YES/UP leg; NO leg proxy = 1 - fill_price.
        down_fill_proxy = 1.0 - fill_price
        meta["fill_price"] = fill_price
        meta["entry_cap_down"] = entry_cap_down
        meta["entry_floor_down"] = entry_floor_down
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

    confidence_score = float(abs(p_btc_pure - 0.5) * 2.0)
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
        entry_reason="v9_3_btc_down_solo_pass",
        skip_reason=None,
        metadata=meta,
    )
