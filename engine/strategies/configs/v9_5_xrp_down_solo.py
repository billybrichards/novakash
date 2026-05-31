"""v9_5_xrp_down_solo — DOWN-only XRP 5m strategy on probability_lgb_v9_5_xrp (GHOST).

Fires ONLY on the DOWN side reading probability_lgb_v9_5_xrp — the PURE column
for the v9.5.1 XRP retrain (481K OOF records, 2026-05-25). The v9.5.1 retrain
refreshed the model behind this SAME column — there is NO separate _pure column
for XRP. PURE values now emit to probability_lgb_v9_5_xrp directly.
Same column as v9_5_xrp_up_solo (UP at p >= 0.82).

Asset: XRP only. Strategy will SKIP defensively on any non-XRP surface.

THRESHOLD RATIONALE (Billy's v9.5.1 XRP OOF sweep, 2026-05-25 — RDS note #710):
  DOWN @ p <= 0.15 (= 0.85 confidence): 90.6% WR  n=48K
  DOWN @ p <= 0.10 (= 0.90 confidence): 93.7% WR  n=27K  ← operating point
  Operating point: down_threshold=0.10 (93.7% WR n=27K).
  down_threshold=0.15 available via strategy_runtime_overrides for more volume
  (90.6% WR n=48K), at the cost of ~3pp WR.

WHY DOWN @ 0.10?
  93.7% WR at n=27K OOF is above Billy's 90% steady-state target. The n=27K
  sample is sufficient for statistical confidence. The tighter threshold is
  appropriate for conservative first DOWN deployment — shadow soak at $5 before
  any promotion.

CLOSES XRP DOWN COVERAGE GAP:
  Prior to this strategy, XRP DOWN coverage existed only via the 15m classifier
  (v7_15m_sniper_xrp, v_xrp_15m_classifier_top10/top20). This adds 5m DOWN
  coverage using the v9.5.1 PURE model probabilities.

CONSECUTIVE TICK CONFIRMATION (min_consecutive_pass_ticks=2):
  DOWN is a new direction for this asset. The 2-tick gate adds conservative
  confirmation before first live fire. (v9_5_xrp_up_solo uses 1.)

Direction-aware fill-band gate (RDS note #664, 2026-05-25):
  - DOWN (NO): skip if fill > entry_cap_down (default 0.85)
               High-fill NO means YES would be < 0.15 — likely resolution-locked
               or ghost market. Applied to DOWN fires.
  - UP  (YES): entry_floor_up present for YAML parity but UNUSED — no UP fires.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for shadow window.

References:
  RDS note #710 — v9.5.1 XRP OOF table (2026-05-25), DOWN sweep data
  RDS note #664 — direction-aware fill-band gate spec
  data_surface.py line 371 — probability_lgb_v9_5_xrp definition
  Sister strategy:  v9_5_xrp_up_solo (UP p>=0.82, 1-tick, $5 max)
  Sibling strategies: v9_5_xrp_blend (UP p>=0.82 / DOWN p<=0.20, eval [60,240])
                      v9_5_xrp_tight_blend (UP p>=0.95 / DOWN p<=0.05)
  Engine precedent: v9_2_eth_solo.py (PR #604), v9_5_xrp_up_solo.py (PR #606)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_xrp_down_solo"
_VERSION = "1.0.0"

# Operating point per RDS note #710 (v9.5.1 XRP OOF sweep, 2026-05-25).
# p <= 0.10 = DOWN @ 0.90 confidence: 93.7% WR n=27K.
# NO up threshold — UP side left to v9_5_xrp_up_solo.
_DEFAULT_DOWN_THRESHOLD = 0.10

# Eval-offset band — mirrors v9_5_xrp_up_solo.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Fill-band gate defaults (RDS note #664).
# entry_cap_down: block NO fills above 0.85 (ghost markets with near-zero YES).
# entry_floor_up: present for YAML parity but UNUSED — no UP fires here.
_DEFAULT_ENTRY_CAP_DOWN = 0.85    # skip DOWN/NO fires if NO fill > 0.85 (RDS #664)
_DEFAULT_ENTRY_FLOOR_UP = 0.70    # UNUSED — no UP fires in this strategy
_DEFAULT_ENTRY_CAP = 0.90
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 2     # 2-tick gate for conservative DOWN confirmation
_DEFAULT_ASSET = "XRP"

# Consecutive-tick state. Module-local so this strategy's state cannot
# collide with sibling strategies (v9_5_xrp_blend, v9_5_xrp_up_solo, etc.).
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


def evaluate_v9_5_xrp_down_solo(surface: "FullDataSurface") -> StrategyDecision:
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

    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_xrp": p_xrp,
        "eval_offset": eval_offset,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # DOWN-only strategy. UP side left to v9_5_xrp_up_solo.
    if p_xrp <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    meta["direction"] = direction

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Default=2 for conservative
    # DOWN direction confirmation. Mirrors v9_2_eth_solo / v9_5_xrp_up_solo.
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
    # DOWN (NO): skip if fill > entry_cap_down (default 0.85).
    # UP  (YES): entry_floor_up is present in YAML for parity but UNUSED here
    #            (there are no UP fires in this DOWN-only strategy).
    # Note: unlike the tickformer + LGB peers which derive
    # `down_fill_proxy = 1 - fill_price` (because their `fill_price` is the YES
    # leg), this strategy's `fill_price` is ALREADY the NO leg (sourced from
    # `clob_down_ask` directly). The entry_cap_down / entry_floor_down checks
    # below therefore compare `fill_price` raw — no inversion needed. See PR #637.
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        # For DOWN we look at clob_down_ask as the NO fill proxy.
        fill_price = getattr(surface, "clob_down_ask", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        entry_cap_down = float(_gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN))
        _efd_raw = _gp._lookup("entry_floor_down", None, None)
        entry_floor_down = float(_efd_raw) if _efd_raw is not None else None
        meta["fill_price"] = fill_price
        meta["entry_cap_down"] = entry_cap_down
        meta["entry_floor_down"] = entry_floor_down
        if direction in ("DOWN", "NO") and fill_price > entry_cap_down:
            return _skip(
                f"fill_above_down_cap:{fill_price:.3f}>{entry_cap_down:.3f}",
                meta,
            )
        if (
            direction in ("DOWN", "NO")
            and entry_floor_down is not None
            and fill_price < entry_floor_down
        ):
            return _skip(
                f"entry_floor_down ({fill_price:.3f} < {entry_floor_down})",
                meta,
            )

    # confidence_score: distance from neutral 0.5 (scaled to [0,1]).
    # For DOWN: p_xrp is low; distance = |p_xrp - 0.5| * 2.
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
        entry_reason="v9_5_xrp_down_solo_pass",
        skip_reason=None,
        metadata=meta,
    )
