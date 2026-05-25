"""v9_3_btc_pure_up_solo — UP-only BTC 5m strategy on probability_lgb_v9_3_btc_pure (GHOST).

Fires ONLY on the UP side reading probability_lgb_v9_3_btc_pure — the PURE LGB+iso
output BEFORE the blend_ensemble step in app/v2_scorer.py (data_surface.py line 349).
The existing BLEND column (probability_lgb_v9_3_btc) is capped at ~0.916 due to
TimesFM HF classifier saturation; the PURE column reaches the full 0-1 range,
enabling the high-conviction UP tail that was invisible on BLEND.

Asset: BTC only. Strategy will SKIP defensively on any non-BTC surface.

WHY UP-ONLY?
  v9.3.1 overnight OOF retrain (468K records, 2026-05-25) shows strong UP signal:
    UP p >= 0.80 conf: 91.3% WR n=115K
    UP p >= 0.85 conf: 94.3% WR n=90K  ← PRIMARY THRESHOLD (this strategy)
    UP p >= 0.90 conf: 95.2% WR n=61K  (tighten via runtime_overrides)
  The DOWN side is covered by companion strategy v9_3_btc_down_solo (p <= 0.20,
  87.0% WR n=130K). Both read probability_lgb_v9_3_btc_pure but fire in opposite
  directions with no threshold overlap — no market conflict.

COVERAGE GAP THIS FILLS:
  v9_3_btc_pure_lgb fires UP only when p >= 0.935. This strategy fires UP when
  p is in [0.85, 0.935) — a zone that v9_3_btc_pure_lgb misses but the v9.3.1
  OOF confirms at 94.3% WR (n=90K). No fire overlap with v9_3_btc_pure_lgb at
  the same window.

Operating point (v9.3.1 OOF retrain, 468K records, 2026-05-25):
  - UP when probability_lgb_v9_3_btc_pure >= 0.85
           -> 94.3% WR n=90K  (UP @ 0.85 confidence)
  - NO DOWN side — covered by v9_3_btc_down_solo (p<=0.20, 87.0% WR n=130K)
  - eval_offset in [60, 180]  — best band consistent with v9_2_eth_solo sweep
  - min_consecutive_pass_ticks=2  (conservative for new column)

Direction-aware fill-band gate (RDS note #664, 2026-05-25):
  - UP  (YES): skip if fill < entry_floor_up (default 0.65)
               skip if fill >= entry_cap (default 0.90 — near-resolved market)
  - DOWN (NO): entry_cap_down present in YAML for parity but UNUSED here
               (there are no DOWN fires in this UP-only strategy).

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for shadow window.

References:
  v9.3.1 OOF retrain (2026-05-25) — 468K records, UP p>=0.85 = 94.3% WR n=90K
  data_surface.py line 349 — probability_lgb_v9_3_btc_pure definition
  RDS note #664 — direction-aware fill-band gate spec
  Sibling strategy: v9_3_btc_down_solo (DOWN p<=0.20, 87.0% WR n=130K)
  v9_3_btc_pure_lgb: UP p>=0.935 / DOWN p<=0.065 (also reads PURE column)
  Engine precedent: v9_5_xrp_up_solo.py (UP-only solo pattern)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_3_btc_pure_up_solo"
_VERSION = "1.0.0"

# v9.3.1 OOF retrain operating point (468K records, 2026-05-25).
# UP p >= 0.85 = 94.3% WR n=90K (UP @ 0.85 confidence).
# Tighten via runtime_overrides: p>=0.90 = 95.2% WR n=61K.
# Loosen: p>=0.80 = 91.3% WR n=115K (more fire rate, lower precision).
# NO down threshold — DOWN side covered by v9_3_btc_down_solo.
_DEFAULT_UP_THRESHOLD = 0.85

# Best eval-offset band from 5-way sweep (consistent with v9_2_eth_solo,
# 2026-05-25): [60, 180] avoids the weak Δ=240s tail.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Fill-band gate defaults (RDS note #664).
# entry_floor_up: skip UP/YES fires if fill < 0.65 (ghost markets / low liquidity).
# entry_cap_down: present for YAML parity but UNUSED — no DOWN fires here.
_DEFAULT_ENTRY_FLOOR_UP = 0.65    # skip UP/YES fires if fill < 0.65 (universal floor)
_DEFAULT_ENTRY_CAP_DOWN = 0.90    # UNUSED — no DOWN fires in this strategy
_DEFAULT_ENTRY_CAP = 0.90         # skip UP/YES fills >= 0.90 (near-resolved)
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 2     # conservative for new column
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Module-local so this strategy's state cannot
# collide with sibling strategies (v9_3_btc_pure_lgb, v9_3_btc_blend,
# v9_3_btc_tight_blend, v9_3_btc_down_solo).
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


def evaluate_v9_3_btc_pure_up_solo(surface: "FullDataSurface") -> StrategyDecision:
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

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_3_btc_pure": p_btc_pure,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # UP-only strategy. DOWN side covered by v9_3_btc_down_solo (p<=0.20).
    if p_btc_pure >= up_threshold:
        direction = "UP"
    else:
        return _skip("conviction_below_threshold", meta)

    meta["direction"] = direction

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Conservative default of 2.
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
    # UP (YES): skip if fill < entry_floor_up (default 0.65) — ghost markets.
    #           skip if fill >= entry_cap (default 0.90) — near-resolved.
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
        entry_cap_check = float(_gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP))
        meta["entry_cap_used"] = entry_cap_check
        if direction in ("UP", "YES") and fill_price >= entry_cap_check:
            return _skip(
                f"fill_above_up_cap:{fill_price:.3f}>={entry_cap_check:.3f}",
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
        entry_reason="v9_3_btc_pure_up_solo_pass",
        skip_reason=None,
        metadata=meta,
    )
