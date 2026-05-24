"""v9_5_eth_pure_lgb — PURE LGB+iso v9.5 ETH 5m strategy (GHOST).

Reads the NEW `probability_lgb_v9_5_eth_pure` field — the un-blended
LGB → isotonic output emitted directly by the v9.5 ETH booster (sibling to
the LIVE-served blend column `probability_lgb_v9_5_eth` which mixes in the
TimesFM HF classifier and is capped at ~0.92 by the classifier's
saturation at ~0.84 — see RDS notes #631, #632, the "blend bug" discovery).

Asset: ETH only. Strategy will SKIP on any non-ETH surface (defensive guard
— the v9.5 ETH model is calibrated for ETH only; firing on BTC/XRP would
be undefined behaviour).

Criteria (walk-forward CV, 5×4d, 16d OOF — see
/tmp/v9_5_eth_walkforward_results.md):
  - UP   when probability_lgb_v9_5_eth_pure >= 0.915
            -> 90.3% WR on n=1330 (Wilson LB 88.6%), ~83.1 fires/day
  - DOWN when probability_lgb_v9_5_eth_pure <= 0.095
            -> 90.4% WR on n=1416 (Wilson LB 88.7%), ~88.5 fires/day
  - eval_offset in [60, 210]  (drops the weaker Δ=240s tail — see Δ-table
    in the walk-forward note; the Δ=30-180s band sits at 92-98% WR while
    Δ=240s sags to ~82-85%)
  - min_consecutive_pass_ticks=1  (single qualifying tick is enough; CV
    already de-duplicated to one outcome per window)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window; raise once GHOST soak confirms the CV numbers.

Companion blend strategy (renamed in the same PR):
  v9_5_eth_blend (née v9_5_eth_raw_lgb — reads the blended column at the
                  tighter 0.96/0.04 operating point). The two strategies
                  read DIFFERENT probability fields and have INDEPENDENT
                  consecutive-tick state (module-local _consec_state).

References:
  - RDS notes #631, #632 — blend bug discovery.
  - Walk-forward CV results: /tmp/v9_5_eth_walkforward_results.md.
  - Sibling timesfm PR: feat/v9_5_eth_pure_emission (forthcoming).
  - Engine precedent: configs/v9_5_eth_raw_lgb.py (the blend variant, now
    being renamed to v9_5_eth_blend in this same PR).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_5_eth_pure_lgb"
_VERSION = "1.0.0"

# Walk-forward CV operating point per /tmp/v9_5_eth_walkforward_results.md.
_DEFAULT_UP_THRESHOLD = 0.915
_DEFAULT_DOWN_THRESHOLD = 0.095

# eval_offset [60, 210] drops the weak Δ=240s tail (Δ=240s WR 82-85% vs
# Δ=30-180s WR 92-98%). Keeps ~94% of fires at materially higher WR.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 210

_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Module-local so this strategy's consecutive-tick state cannot collide with
# sibling strategies (v9_5_eth_blend née v9_5_eth_raw_lgb, v9_2_eth_raw_lgb,
# v9_2_eth_late_band_AB_blend, v9_2_eth_down_late).
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


def evaluate_v9_5_eth_pure_lgb(surface: "FullDataSurface") -> StrategyDecision:
    p_eth_pure = getattr(surface, "probability_lgb_v9_5_eth_pure", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=ETH but
    # belt-and-braces — refuse to fire if the registry ever wires a non-ETH
    # surface in. The v9.5 ETH model is not calibrated for other assets.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_eth_pure is None:
        return _skip(
            "v9_5_eth_pure_model_not_loaded",
            {"probability_lgb_v9_5_eth_pure": None},
        )

    p_eth_pure = float(p_eth_pure)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_5_eth_pure": p_eth_pure,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_eth_pure >= up_threshold:
        direction = "UP"
    elif p_eth_pure <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks).
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

    confidence_score = float(abs(p_eth_pure - 0.5) * 2.0)
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
        entry_reason="v9_5_eth_pure_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
