"""v9_3_btc_pure_lgb — v9.3 BTC PURE LGB single-head strategy (GHOST).

Reads the NEW probability_lgb_v9_3_btc_pure field — the post-isotonic-
calibration LGB output BEFORE the blend_ensemble step in app/v2_scorer.py.
The existing probability_lgb_v9_3_btc (blended) field is untouched; this
strategy is a sibling of v9_3_btc_raw_lgb / v9_3_btc_tight that explicitly
opts in to the PURE column to bypass the TimesFM HF classifier head
throttling (which caps the blend at ~0.916 due to classifier saturation
at ~0.84; see RDS notes #618, #631, #632).

Asset: BTC only. Strategy will SKIP on any non-BTC surface (defensive
guard — the v9.3 BTC model is calibrated for BTC only; firing on ETH/XRP
would be undefined behaviour).

Criteria (walk-forward CV at /tmp/btc_walkforward_results.md):
  - UP   when probability_lgb_v9_3_btc_pure >= 0.935  -> 90.3% WR (1725
    windows, Wilson LB 88.8%, ~82 fires/day over 21d corpus)
  - DOWN when probability_lgb_v9_3_btc_pure <= 0.065  -> 90.4% WR (1590
    windows, Wilson LB 88.9%, ~76 fires/day)
  - eval_offset in [60, 240]
  - min_consecutive_pass_ticks=1 (matches v9_3_btc_raw_lgb so shadow-vs-LIVE
    comparisons stay like-for-like)

~4x more fires than the v9_3_btc_raw_lgb (blend) variant at the same 90%
WR target. v9.3 fires are <0.07 correlated with v9.1/v9.2/v12 fires →
strong diversification when stacked with the v9_2_v12_combo_pure sibling
strategy in this PR.

Sized by Kelly (collateral_pct) capped at max_position_usd via YAML.
GHOST mode by default — Billy promotes manually per
feedback_no_auto_promote.md. $5 max_position_usd for the initial shadow
window; raise once shadow data validates the thresholds.

Sibling timesfm PR: #163 (feat/v9_3_btc_pure_lgb_emission).
Sibling engine strategy: v9_2_v12_combo_pure (BTC AND-combo).
Engine precedent: feat/v9_5_eth_pure_lgb_strategy (ETH variant).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_3_btc_pure_lgb"
_VERSION = "1.0.0"

# Walk-forward CV operating point per /tmp/btc_walkforward_results.md.
_DEFAULT_UP_THRESHOLD = 0.935
_DEFAULT_DOWN_THRESHOLD = 0.065

# eval_offset band — full [60, 240] per walk-forward (v9.3 PURE holds
# 90%+ WR across the entire band, unlike the v9.5 ETH PURE which sags
# in the Delta=240 tail).
_DEFAULT_EVAL_OFFSET_MIN = 60
_DEFAULT_EVAL_OFFSET_MAX = 240

# Higher entry/gtc caps than the blend siblings — the PURE column reaches
# 1.000 (vs 0.916 blend cap) so we need headroom for the higher-conviction
# tail to keep computed Kelly fractions from clipping.
_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96

# 1-tick consecutive gate matches v9_3_btc_raw_lgb — keep selectivity
# profile identical so the shadow-vs-LIVE delta is purely the model swap
# (blend → pure).
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_ASSET = "BTC"

# Consecutive-tick state. Maps (window_ts, direction) -> (count, last_seen_ts).
# Module-local so this strategy's state cannot collide with the blend
# siblings (v9_3_btc_raw_lgb, v9_3_btc_tight) or with v9_2_v12_combo_pure.
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


def evaluate_v9_3_btc_pure_lgb(surface: "FullDataSurface") -> StrategyDecision:
    p_pure = getattr(surface, "probability_lgb_v9_3_btc_pure", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)

    # Defensive asset guard. Strategy is registered with asset=BTC but
    # belt-and-braces — refuse to fire if the registry ever wires a non-BTC
    # surface in.
    expected_asset = _gp.get_str("expected_asset", None, _DEFAULT_ASSET)
    if asset != expected_asset:
        return _skip(
            "wrong_asset",
            {"asset": asset, "expected_asset": expected_asset},
        )

    if p_pure is None:
        return _skip(
            "v9_3_btc_pure_model_not_loaded",
            {"probability_lgb_v9_3_btc_pure": None},
        )

    p_pure = float(p_pure)

    up_threshold = _gp.get_float("up_threshold", None, _DEFAULT_UP_THRESHOLD)
    down_threshold = _gp.get_float("down_threshold", None, _DEFAULT_DOWN_THRESHOLD)
    eval_min = _gp.get_int("eval_offset_min", None, _DEFAULT_EVAL_OFFSET_MIN)
    eval_max = _gp.get_int("eval_offset_max", None, _DEFAULT_EVAL_OFFSET_MAX)

    meta = {
        "probability_lgb_v9_3_btc_pure": p_pure,
        "eval_offset": eval_offset,
        "up_threshold": up_threshold,
        "down_threshold": down_threshold,
        "eval_offset_min": eval_min,
        "eval_offset_max": eval_max,
        "asset": asset,
    }

    if eval_offset is None or eval_offset < eval_min or eval_offset > eval_max:
        return _skip("outside_eval_band", meta)

    if p_pure >= up_threshold:
        direction = "UP"
    elif p_pure <= down_threshold:
        direction = "DOWN"
    else:
        return _skip("conviction_below_threshold", meta)

    # N-consecutive-tick confirmation gate (runtime-tunable via
    # gate_params.min_consecutive_pass_ticks). Mirrors v9_3_btc_raw_lgb.
    window_ts = getattr(surface, "window_ts", None)
    min_consec = _gp.get_int(
        "min_consecutive_pass_ticks", None, _DEFAULT_MIN_CONSEC_TICKS
    )
    consec_count = _bump_and_check(window_ts or 0, direction, min_consec)
    meta["consec_tick_count"] = consec_count
    meta["min_consecutive_pass_ticks"] = min_consec
    if consec_count < min_consec:
        return _skip("awaiting_consec_ticks", meta)

    confidence_score = float(abs(p_pure - 0.5) * 2.0)
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
        entry_reason="v9_3_btc_pure_lgb_pass",
        skip_reason=None,
        metadata=meta,
    )
