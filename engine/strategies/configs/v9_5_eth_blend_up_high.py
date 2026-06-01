"""v9_5_eth_blend_up_high — UP-HIGH ceiling tier of the v9.5 ETH BLEND
ladder (GHOST).

Fires UP when probability_lgb_v9_5_eth falls inside the band
[up_threshold, up_threshold_max) — default [0.86, 2.0). The upper bound
is an impossible value (calibrated probability is in [0, 1]), so this
strategy effectively fires on any p >= 0.86. BLEND output caps at
~0.9214 due to classifier-head saturation, so in practice this strategy
fires on the very-high-conviction blended UP signals in the [0.86, 0.92]
regime.

See sibling v9_5_eth_blend_up_low.py for the full ladder layout and
design notes.

Band [0.86, 0.88): n=26 OOF, 100% WR, Wilson LB 87.1%. Cumulative
p >= 0.86 is even better — the upper-band saturation region remains
high-WR.

UP-only by design. Asset: ETH only. Reads the EXISTING
probability_lgb_v9_5_eth column. GHOST mode by default. $5
max_position_usd.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_eth_blend_up_high"
_VERSION = "1.0.0"

# Ceiling tier: lower bound 0.86, upper bound impossible (2.0 — calibrated
# p never exceeds 1.0). Effectively a one-sided p >= 0.86 check.
_DEFAULT_UP_THRESHOLD = 0.86
_DEFAULT_UP_THRESHOLD_MAX = 2.0

_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 240

_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Module-local consecutive-tick state — isolated from sibling tiers.
_consec_state: dict[tuple[int, str], tuple[int, float]] = {}
_MAX_GAP_S = 5.0


def _bump_and_check(window_ts: int, direction: str, min_ticks: int) -> int:
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
      1. ``eval_offset_min_X`` / ``eval_offset_max_X`` in active gate-params bag.
      2. Else ``sym_min`` / ``sym_max`` — the already-resolved symmetric band.

    Back-compat: when no direction-specific key is present, sym values are returned
    unchanged — identical to the previous symmetric-band behaviour.
    Mirrors _tickformer_base._resolve_band_for_direction (PR #635).
    """
    suffix = direction.lower()  # 'up' or 'down'
    band_min = _gp.get_int(f"eval_offset_min_{suffix}", None, sym_min)
    band_max = _gp.get_int(f"eval_offset_max_{suffix}", None, sym_max)
    return (band_min, band_max)


def evaluate_v9_5_eth_blend_up_high(surface: "FullDataSurface") -> StrategyDecision:
    p_eth = getattr(surface, "probability_lgb_v9_5_eth", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_eth is None:
        return _skip(
            "v9_5_eth_model_not_loaded",
            {"probability_lgb_v9_5_eth": None},
        )

    p_eth = float(p_eth)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    up_threshold_max = _gp.get_float(
        "up_threshold_max", None, _DEFAULT_UP_THRESHOLD_MAX
    )
    sym_eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    sym_eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_eth": p_eth,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "up_threshold_max": up_threshold_max,
        "eval_offset_min": sym_eval_min,
        "eval_offset_max": sym_eval_max,
        "asset": asset,
    }

    if eval_offset is None:
        return _skip("outside_eval_band", meta)

    # UP-only band check (ceiling tier — upper bound 2.0 is impossible).
    if up_threshold <= p_eth < up_threshold_max:
        direction = "UP"
    else:
        return _skip("outside_conviction_band", meta)

    eval_min, eval_max = _resolve_eval_band(
        direction, sym_min=sym_eval_min, sym_max=sym_eval_max
    )
    meta["eval_offset_min_resolved"] = eval_min
    meta["eval_offset_max_resolved"] = eval_max
    if eval_offset < eval_min or eval_offset > eval_max:
        return _skip(f"outside_eval_band_{direction} ({eval_min}-{eval_max})", meta)

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

    confidence_score = float(abs(p_eth - 0.5) * 2.0)
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
        entry_reason="v9_5_eth_blend_up_high_pass",
        skip_reason=None,
        metadata=meta,
    )
