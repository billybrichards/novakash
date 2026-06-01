"""v9_5_xrp_pure_lgb — PURE LGB+iso v9.5 XRP 5m strategy (GHOST, bidirectional).

Reads `probability_lgb_v9_5_xrp_pure` — the dedicated PURE column for the
XRP v9.5 booster + isotonic calibrator, distinct from the blended
`probability_lgb_v9_5_xrp` column used by v9_5_xrp_blend / v9_5_xrp_up_solo
/ v9_5_xrp_down_solo.

Bidirectional:
  - UP   when probability_lgb_v9_5_xrp_pure >= up_threshold   (default 0.92)
  - DOWN when probability_lgb_v9_5_xrp_pure <= down_threshold (default 0.06)

Sweep findings (RDS notes #788, #790 — XRP PURE column sweep, 2026-05-31):
  DOWN @ p <= 0.06: LCL 91.2% n=241, ~44 fires/day
     → existing v9_5_xrp_down_solo (blend col, thr 0.44) sits at LCL 85.5%
       n=389. The PURE column gives a stronger DOWN signal at a tighter
       threshold with acceptable volume.
  UP  @ p >= 0.92: conservative starting point — operator tunes via
     strategy_runtime_overrides once ghost-soak data accumulates.

Motivation: unifies PURE-column access for BOTH UP and DOWN on a single
strategy, so either side can be promoted independently via
strategy_runtime_overrides.params without code changes — same operator
workflow as v9_5_eth_pure_lgb.

Asset: XRP only. Strategy will SKIP on any non-XRP surface (defensive guard
— the v9.5 XRP PURE model is calibrated for XRP only; firing on BTC/ETH
would be undefined behaviour).

Consecutive-tick gate:
  Default min_consecutive_pass_ticks=2 — conservative confirmation for a new
  PURE-column operating point. Mirrors v9_5_xrp_down_solo. Operator can lower
  to 1 via sro once ghost soak confirms stability.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial shadow
window; raise once GHOST soak confirms the sweep numbers.

Per-direction eval-band support (feat/v9_5-direction-split-bands, plan #806):
  The symmetric band can be split per direction using:
    eval_offset_min_up  / eval_offset_max_up   — UP-specific band
    eval_offset_min_down / eval_offset_max_down — DOWN-specific band
  Back-compat waterfall: direction key → symmetric key → YAML default.
  Sweep analysis (RDS note #806): UP optimal at band 90-180, DOWN optimal
  at 30-90 (DISJOINT — impossible to serve both with one symmetric band).
  Example SRO to unlock both directions independently:
    eval_offset_min_up=90,   eval_offset_max_up=180
    eval_offset_min_down=30, eval_offset_max_down=90

References:
  - RDS notes #788, #790 — XRP PURE column sweep findings (2026-05-31).
  - Sibling strategies: v9_5_eth_pure_lgb (ETH PURE bidirectional template),
                        v9_5_xrp_blend (blend col, UP/DOWN),
                        v9_5_xrp_down_solo (blend col, DOWN-only),
                        v9_5_xrp_up_solo (blend col, UP-only).
  - data_surface.py line 385 — probability_lgb_v9_5_xrp_pure field.
  - Engine precedent: PR #604 (v9_2_eth_solo), PR #606 (v9_5_xrp_up_solo).
  - Direction-split reference: configs/_tickformer_base._resolve_band_for_direction (PR #635).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_xrp_pure_lgb"
_VERSION = "1.0.0"

# Operating point per RDS notes #788, #790 (XRP PURE column sweep, 2026-05-31).
# UP threshold: conservative 0.92 starting point — tune via sro after ghost soak.
# DOWN threshold: 0.06 → LCL 91.2% n=241 ~44/day (stronger than blend col DOWN).
_DEFAULT_UP_THRESHOLD = 0.92
_DEFAULT_DOWN_THRESHOLD = 0.06

# Eval-offset band. Default [60, 180] matches the canonical RDS #684 band used
# by v9_5_xrp_down_solo and v9_5_xrp_up_solo. Operator can widen or narrow via
# sro (e.g. eval_offset_max=210 to include the wider tail if sweep supports it).
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 180

# Separate per-direction entry caps (mirrors v9_5_xrp_down_solo pattern).
# UP  (YES): entry_cap_up — cap on YES fill.
# DOWN (NO): entry_cap_down — cap on NO fill (high-fill NO = resolution-locked).
_DEFAULT_ENTRY_CAP_UP = 0.92
_DEFAULT_ENTRY_CAP_DOWN = 0.85
_DEFAULT_ENTRY_CAP = 0.90          # general entry_cap fallback (StrategyDecision field)
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 2       # conservative 2-tick gate for new PURE column
_DEFAULT_ASSET = "XRP"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Module-local so this strategy's consecutive-tick state cannot collide with
# sibling strategies (v9_5_xrp_blend, v9_5_xrp_up_solo, v9_5_xrp_down_solo,
# v9_5_eth_pure_lgb, etc.).
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


def _resolve_eval_band(direction: str, *, sym_min: int, sym_max: int) -> tuple[int, int]:
    """Return ``(band_min, band_max)`` for ``direction`` using the waterfall:

    For each direction X ∈ {'up', 'down'}:
      1. ``eval_offset_min_X`` / ``eval_offset_max_X`` in active gate-params bag
         (runtime override or YAML direction-specific key — highest precedence).
      2. Else ``sym_min`` / ``sym_max`` — the already-resolved symmetric band
         (from runtime override bare key → YAML bare key → default).

    Back-compat guarantee: when neither ``eval_offset_max_up`` nor
    ``eval_offset_max_down`` is present in the active params bag, ``sym_min``
    / ``sym_max`` are returned unchanged, preserving identical behaviour for
    all existing strategies and runtime overrides that lack the new keys.

    Mirrors _tickformer_base._resolve_band_for_direction but uses the
    ``eval_offset_min/max`` key prefix (not ``eval_offset_remaining_min/max``)
    to match the existing v9.5 key naming convention.
    """
    suffix = direction.lower()  # 'up' or 'down'
    band_min = _gp.get_int(f"eval_offset_min_{suffix}", None, sym_min)
    band_max = _gp.get_int(f"eval_offset_max_{suffix}", None, sym_max)
    return (band_min, band_max)


def evaluate_v9_5_xrp_pure_lgb(surface: "FullDataSurface") -> StrategyDecision:
    p_xrp_pure = getattr(surface, "probability_lgb_v9_5_xrp_pure", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)
    window_ts = getattr(surface, "window_ts", None)

    # Defensive asset guard. The strategy is registered with asset=XRP but
    # belt-and-braces — refuse to fire if the registry ever wires a non-XRP
    # surface in. The v9.5 XRP PURE model is not calibrated for other assets.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_xrp_pure is None:
        return _skip(
            "v9_5_xrp_pure_model_not_loaded",
            {"probability_lgb_v9_5_xrp_pure": None, "window_ts": window_ts},
        )

    p_xrp_pure = float(p_xrp_pure)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    # Resolve symmetric band first (used as fallback in the direction-split waterfall).
    sym_eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    sym_eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_xrp_pure": p_xrp_pure,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": sym_eval_min,
        "eval_offset_max": sym_eval_max,
        "asset": asset,
        "window_ts": window_ts,
    }

    # Guard: eval_offset absent → can't evaluate any band.
    if eval_offset is None:
        return _skip("outside_eval_band", meta)

    # Direction resolution first — needed to select the per-direction band.
    if p_xrp_pure >= up_threshold:
        direction = "UP"
    elif p_xrp_pure <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # Direction-aware band check (feat/v9_5-direction-split-bands, plan #806).
    # Waterfall: eval_offset_min/max_{up,down} → sym eval_offset_min/max → default.
    # Back-compat: when no direction-specific key is set, sym values are used unchanged.
    eval_min, eval_max = _resolve_eval_band(
        direction, sym_min=sym_eval_min, sym_max=sym_eval_max
    )
    meta["eval_offset_min_resolved"] = eval_min
    meta["eval_offset_max_resolved"] = eval_max
    if eval_offset < eval_min or eval_offset > eval_max:
        return _skip(f"outside_eval_band_{direction} ({eval_min}-{eval_max})", meta)

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Default=2 for conservative
    # confirmation of a new PURE-column operating point.
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

    confidence_score = float(abs(p_xrp_pure - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    # Direction-aware fill-band gate (RDS note #664, 2026-05-25).
    # Caps: skip if fill is too HIGH (entry_cap_up / entry_cap_down).
    # Floors: skip if fill is too LOW (entry_floor_up / entry_floor_down).
    # Defaults are permissive — strats NOT setting floor fields are unaffected.
    fill_price = getattr(surface, "fill_price", None)
    if direction == "DOWN" and fill_price is None:
        fill_price = getattr(surface, "clob_down_ask", None)
    elif direction == "UP" and fill_price is None:
        fill_price = getattr(surface, "clob_up_ask", None)
    if fill_price is not None:
        fill_price = float(fill_price)
        meta["fill_price"] = fill_price
        if direction == "DOWN":
            entry_cap_down = float(
                _gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN)
            )
            meta["entry_cap_down"] = entry_cap_down
            if fill_price > entry_cap_down:
                return _skip(
                    f"fill_above_down_cap:{fill_price:.3f}>{entry_cap_down:.3f}",
                    meta,
                )
            # Floor gate for DOWN: reject if NO fill is too low (CLOB price-improvement).
            _efd_raw = _gp._lookup("entry_floor_down", None, None)
            entry_floor_down = float(_efd_raw) if _efd_raw is not None else None
            meta["entry_floor_down"] = entry_floor_down
            # fill_price here is the NO-leg price directly (clob_down_ask / fill_price).
            if entry_floor_down is not None and fill_price < entry_floor_down:
                return _skip(
                    f"fill_below_down_floor:{fill_price:.3f}<{entry_floor_down:.3f}",
                    meta,
                )
        elif direction == "UP":
            entry_cap_up = float(
                _gp.get_float("entry_cap_up", None, _DEFAULT_ENTRY_CAP_UP)
            )
            meta["entry_cap_up"] = entry_cap_up
            if fill_price > entry_cap_up:
                return _skip(
                    f"fill_above_up_cap:{fill_price:.3f}>{entry_cap_up:.3f}",
                    meta,
                )
            # Floor gate for UP: reject if YES fill is too low (CLOB price-improvement).
            entry_floor_up = float(_gp.get_float("entry_floor_up", None, 0.0))
            meta["entry_floor_up"] = entry_floor_up
            if fill_price < entry_floor_up:
                return _skip(
                    f"fill_below_up_floor:{fill_price:.3f}<{entry_floor_up:.3f}",
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
        entry_reason="v9_5_xrp_pure_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
