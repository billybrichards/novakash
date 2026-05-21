"""v9_2_eth_raw_lgb — clean raw v9.2-style ETH 5m signal strategy (GHOST).

Fires on raw probability_lgb_v9_2_eth without v9_ensemble base-gate delegation.
No cohort gates, no sister-pair veto, no cell pauses, no blocked hours
(matches v9_2_raw_lgb — the BTC analogue — at first; per-hour blocks can be
layered in once shadow data justifies them).

Asset: ETH only. Strategy will SKIP on any non-ETH surface. Single qualifying
tick is enough (no N-of-M cohort gate, mirrors v9_2_raw_lgb).

Criteria (hub note #550, ETH/XRP v1 corrected WR-at-threshold table):
  - UP   when probability_lgb_v9_2_eth >= 0.85  -> projected WR ~92-94% on test
  - DOWN when probability_lgb_v9_2_eth <= 0.20  -> projected WR ~92-94% on test
  - eval_offset in [60, 150]  (T-60 to T-150 — tighter than BTC's 60-210
    because hub note #550 flagged weak δ=180 for ETH; we drop the weakest
    band entirely)
  - One qualifying tick is enough (no N-of-M cohort gate)

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually
(per feedback_no_auto_promote.md). $5 max_position_usd for the initial
shadow window; raise once shadow data validates the thresholds.

Hub notes: #545 (data inventory), #547 (training-pipeline spec),
#550 (training results + threshold table).
Companion timesfm PR: bg-agent-1 v9.2 ETH/XRP training pipeline.
Engine precedent: feat/v9_2_post_iso_column_and_strategies (PR #555).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_eth_raw_lgb"
_VERSION = "1.0.0"

# Thresholds per hub note #550 recommendation.
_DEFAULT_UP_THRESHOLD = 0.85
_DEFAULT_DOWN_THRESHOLD = 0.20

# Tighter than BTC v9_2_raw_lgb's 60-210 band: drops δ=180 which hub note
# #550 flagged as the weakest tail of the ETH eval-band distribution.
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 150

_DEFAULT_ENTRY_CAP = 0.85
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.90
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "ETH"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Resets when direction changes or the gap between evals exceeds _MAX_GAP_S.
# Mirrors v9_2_raw_lgb._bump_and_check exactly.
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


def evaluate_v9_2_eth_raw_lgb(surface: "FullDataSurface") -> StrategyDecision:
    p_eth = getattr(surface, "probability_lgb_v9_2_eth", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. The strategy is registered with asset=ETH but
    # belt-and-braces — refuse to fire if the registry ever wires a non-ETH
    # surface in. The ETH model is not calibrated for other assets.
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

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_2_raw_lgb.
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
        entry_reason="v9_2_eth_raw_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
