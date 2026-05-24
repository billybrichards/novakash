"""v9_2_eth_late_band_AB_blend — UP-only ETH 5m late-window strategy (GHOST).

Fires on raw probability_lgb_v9_2_eth at the audit-recommended late-window
operating point. UP direction only — DOWN was audited at n=5 (note #627),
too thin to trust.

Asset: ETH only. Strategy will SKIP defensively on any non-ETH surface (the
v9.2 ETH model is calibrated for ETH only).

Criteria (RDS notes #627 + #629):
  - UP   when probability_lgb_v9_2_eth >= 0.82
          -> 14d audit: 92.9% WR on n=211 windows (note #627)
          -> 38h post-PR-#582 re-check: 88.0% WR on n=25
             (Wilson LB 70.0% — small sample, in variance) (note #629)
  - eval_offset in [60, 90]  (T-minus 60-90 seconds to window close —
    SECONDS REMAINING convention; verified against engine source
    `cell_param_overrides.py` and live RDS data)
  - DOWN direction intentionally NOT implemented (audit n=5, too thin)
  - min_consecutive_pass_ticks=1

Why late-band: eval_offset 60-90 corresponds to the last 60-90s before the
5m window closes. Empirically the tightest UP corner of the v9.2 ETH model
per the 14d audit — calibration is much higher at this late-window position
than across the full eval-offset distribution.

No new probability columns needed — probability_lgb_v9_2_eth already exists
in signal_evaluations and is populated by the v9.2 ETH scorer.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window.

Sibling strategy: v9_2_eth_raw_lgb (different op-point — 0.96/0.04 in
eval_offset 120-210, no overlap with this late-band UP corner).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_eth_late_band_AB_blend"
_VERSION = "1.0.0"

# Audit operating point per RDS notes #627 + #629.
_DEFAULT_UP_THRESHOLD = 0.82

# Late-window band: T-minus 60-90 seconds to window close. eval_offset is
# SECONDS REMAINING (verified against cell_param_overrides.py and live RDS).
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 90

_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.93
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Module-local so this strategy's consecutive-tick state cannot collide with
# sibling strategies (v9_2_eth_raw_lgb, v9_2_eth_down_late).
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


def evaluate_v9_2_eth_late_band_AB_blend(surface: "FullDataSurface") -> StrategyDecision:
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
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_2_eth": p_eth,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    # UP-only strategy. DOWN side intentionally not implemented (audit n=5,
    # too thin to trust per RDS note #627).
    if p_eth >= up_threshold:
        direction = "UP"
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
        entry_reason="v9_2_eth_late_band_AB_blend_pass",
        skip_reason=None,
        metadata=meta,
    )
