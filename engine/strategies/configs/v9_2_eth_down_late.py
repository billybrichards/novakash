"""v9_2_eth_down_late — DOWN-only late-window ETH 5m signal strategy (GHOST).

Companion to v9_2_eth_raw_lgb. Reads the same probability_lgb_v9_2_eth field
but operates in a DIFFERENT eval_offset band ([60, 90] — the "late window"
closest to resolution) at a softer DOWN threshold (p <= 0.10). UP gate is
disabled via an impossible threshold (up_threshold: 2.0 in YAML).

Per hub note #550 corrected WR-at-threshold sweep, this band has its own
strong DOWN pocket the main v9_2_eth_raw_lgb strategy ([120, 210], p <= 0.04)
does NOT catch. The two strategies may overlap on the same DOWN-winning
window if its low-prob ticks span both bands — intentional, each strategy
stakes $5 independently. The fires/day projections are NOT additive when
strategies overlap on the same window; expect 22-25 unique DOWN-winning
windows total per day with some windows triggering both strategies.

Criteria:
  - DOWN when probability_lgb_v9_2_eth <= 0.10  -> 93.3% WR / 22 fires/day on test
  - eval_offset in [60, 90]                     -> T-60 to T-90 (last 30s)
  - UP gate disabled (up_threshold: 2.0)
  - One qualifying tick is enough

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML. GHOST
mode by default — Billy promotes manually per feedback_no_auto_promote.md.

Hub notes: #545 (data inventory), #547 (training-pipeline spec),
#550 (granular WR-at-threshold sweep — late-band DOWN pocket source).
Companion strategy: v9_2_eth_raw_lgb.
Engine precedent: feat/v9_2_post_iso_column_and_strategies (PR #555).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_eth_down_late"
_VERSION = "1.0.0"

# Defaults match the YAML; these are fallbacks if gate_params is missing keys.
# UP disabled — calibrated probability is in [0, 1], so 2.0 is unreachable.
_DEFAULT_UP_THRESHOLD = 2.0
_DEFAULT_DOWN_THRESHOLD = 0.10
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 90

_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Independent per-strategy consecutive-tick state. Separate from
# v9_2_eth_raw_lgb._consec_state — these are different strategies firing on
# different bands; their consec counters must not interfere.
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


def evaluate_v9_2_eth_down_late(surface: "FullDataSurface") -> StrategyDecision:
    p_eth = getattr(surface, "probability_lgb_v9_2_eth", None)
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

    # UP gate intentionally unreachable (up_threshold defaults to 2.0).
    # Kept for parity with v9_2_eth_raw_lgb so the gate-pass logic is identical
    # and so YAML overrides could in principle re-enable UP for an experiment.
    if p_eth >= up_threshold:
        direction = "UP"
    elif p_eth <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    window_ts = getattr(surface, "window_ts", None)
    min_consec = _gp.get_int(
        "min_consecutive_pass_ticks", None, _DEFAULT_MIN_CONSEC_TICKS
    )
    consec_count = _bump_and_check(window_ts or 0, direction, min_consec)
    meta["consec_tick_count"] = consec_count
    meta["min_consecutive_pass_ticks"] = min_consec
    if consec_count < min_consec:
        return _skip("awaiting_consec_ticks", meta)

    confidence_score = float(abs(p_eth - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float("collateral_pct", None, _DEFAULT_COLLATERAL_PCT)
    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct
    meta["gtc_cap"] = gtc_cap

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
        entry_reason="v9_2_eth_down_late_pass",
        skip_reason=None,
        metadata=meta,
    )
