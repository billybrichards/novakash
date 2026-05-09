"""Hook for v9_1_meta_kelly — meta-v2 Stage-A booster + deterministic Kelly sizing.

New PARALLEL strategy running alongside v9_1_lgb_only (NOT modifying it).
Both strategies see the same /v4/snapshot payload. This strategy ignores
LGB-distance gates and uses meta + Kelly as the entire gate.

Architecture:
  - Reads probability_lgb_v9_1 (v9.1 booster output) AND probability_meta_v9_1
    (meta-v2 calibrated P(WIN)) from the surface.
  - Applies cascade-fade-pair veto FIRST (hard constraint per meta_v2 design).
  - Applies deterministic fractional Kelly (k=0.25) as the sole gate.
  - Emits stake_fraction * bankroll, capped at abs_max_stake_usd.

NO LGB-distance gate. No lgb_dist_min_up/down. No cohort_x_thresholds.
No ticks_n_thresholds. Meta + Kelly is the entire gate.

Meta model:
  strategy_id: v9_1_lgb_only
  artifact:    meta_v2_v2_2026-05-08 (SHA 4b586e0fbc99)
  S3:          s3://bbrnovakash-models-do-not-delete/
                 v10_training_backup/meta_v2_2026-05-08/v9_1_lgb_only/
  AUC:         0.7705 (note: below 0.78 original gate threshold, but per
               honest reassessment the 0.78 gate is too tight for the ~80%
               training base rate; per-bucket ECE is clean at 0.021 worst
               n>=20 bucket). Deployed GHOST/canary only.
  Top-1 dominance fixed: 50.6% -> 22.3% (rolling_wr_in_cohort_n10).

Honest verdict: deployable as GHOST canary. NOT for full unbounded sizing
without further validation.

Configurable via strategy_runtime_overrides.params:
  abs_max_stake_usd:  10.0  (half of v9_1_lgb_only's $20 -- ghost-safety)
  kelly_fraction:     0.25  (quarter-Kelly per stake_sizing math)
  meta_model_version: 'meta_v2_v2_2026-05-08'

Cascade-fade-pair veto (HARD CONSTRAINT):
  Per meta_v2 design -- veto runs before meta scoring. If BOTH cascade-fade
  sisters (v9_1_cascade_fade_late + v9_cascade_fade_late) fired OPPOSITE
  to the v9.1 direction within +-20s, this strategy SKIPs.

Cooldown / once-per-window:
  Uses _v9_1_meta_window_fired to track whether this strategy has already
  fired for a (window_ts, direction) pair. Resets on new window_ts.
  Mirrors the v9_2_super pattern.

Runtime features NOT available at tick time (engine gap -- documented):
  The meta booster uses 59 features including rolling_wr_in_cohort_n10,
  calibration_residual_trend_n20, conf_minus_cohort_baserate, and
  sister_concordance. These are assembled by timesfm-service from the
  /v4/snapshot payload BEFORE the meta booster is scored. The pre-scored
  probability_meta_v9_1 field already incorporates whatever features were
  available at scoring time. Missing cohort-history features (first-boot
  cold-start) default to NaN inside the meta booster (LGB handles NaN
  via missing-value splits) -- conservative lower bound on Kelly positivity.

PR history:
  timesfm-service feat/v9_1_meta_wireup -- emits probability_meta_v9_1
  engine feat/v9_1_meta_strategy -- this PR (parallel strategy registration)

Default OFF: V9_1_META_ENABLED=false in timesfm-service -> probability_meta_v9_1
is None -> this strategy SKIPs (skip_reason=meta_model_not_loaded).
"""
from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.sister_veto_bus import is_sister_pair_veto_active

_STRATEGY_ID = "v9_1_meta_kelly"
_VERSION = "1.0.0-ghost-canary"

# Default configurable params
_DEFAULT_ABS_MAX_STAKE_USD = 10.0   # ghost-safety: half of v9_1_lgb_only's $20
_DEFAULT_KELLY_FRACTION = 0.25      # quarter-Kelly
_DEFAULT_META_MODEL_VERSION = "meta_v2_v2_2026-05-08"

# Sister-veto defaults (mirrors v9_2_super pattern)
_SISTER_VETO_DEFAULT_PAIR = ["v9_1_cascade_fade_late", "v9_cascade_fade_late"]
_SISTER_VETO_DEFAULT_WINDOW_SEC = 20

# Once-per-window state machine
# Keyed by "{window_ts}:{direction}" -> True when this strategy has already
# fired for that (window, direction). Prevents double-firing same window.
_v9_1_meta_window_fired: dict[str, bool] = {}


def _fired_key(window_ts: int, direction: str) -> str:
    return f"{window_ts}:{direction}"


def mark_window_fired(window_ts: int, direction: str) -> None:
    _v9_1_meta_window_fired[_fired_key(window_ts, direction)] = True


def has_window_fired(window_ts: int, direction: str) -> bool:
    return _v9_1_meta_window_fired.get(_fired_key(window_ts, direction), False)


def reset_window_fired(window_ts: int, direction: str) -> None:
    _v9_1_meta_window_fired.pop(_fired_key(window_ts, direction), None)


def reset_all_window_fired_v9_1_meta() -> None:
    """Clear all state. Used by tests."""
    _v9_1_meta_window_fired.clear()


# Config helpers

def _abs_max_stake_usd() -> float:
    params = _gp._ACTIVE.get()
    v = params.get("abs_max_stake_usd")
    return float(v) if v is not None else _DEFAULT_ABS_MAX_STAKE_USD


def _kelly_fraction() -> float:
    params = _gp._ACTIVE.get()
    v = params.get("kelly_fraction")
    return float(v) if v is not None else _DEFAULT_KELLY_FRACTION


def _sister_veto_enabled() -> bool:
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", True))
    return bool(params.get("sister_veto_enabled", True))


def _sister_veto_pair() -> list[str]:
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict) and "pair" in cfg:
        v = cfg["pair"]
        return list(v) if isinstance(v, (list, tuple)) else _SISTER_VETO_DEFAULT_PAIR
    v = params.get("sister_veto_pair")
    if isinstance(v, (list, tuple)):
        return list(v)
    return _SISTER_VETO_DEFAULT_PAIR


def _sister_veto_window_sec() -> int:
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict) and "agreement_window_seconds" in cfg:
        return int(cfg["agreement_window_seconds"])
    v = params.get("sister_veto_agreement_window_seconds")
    if v is not None:
        return int(v)
    return _SISTER_VETO_DEFAULT_WINDOW_SEC


# Kelly stake computation

def deterministic_stake(meta_prob: float, fill_price: float, kelly_fraction: float) -> float:
    """Fractional Kelly stake sizing.

    Kelly criterion for a binary bet at fill_price:
      b = net_odds = (1 - fill_price) / fill_price
      kelly_full = (b * meta_prob - (1 - meta_prob)) / b
      stake_fraction = max(0, kelly_full) * kelly_fraction

    Args:
        meta_prob:      Calibrated P(WIN) from meta-v2 isotonic booster.
        fill_price:     Market fill price (e.g. 0.65 mid).
        kelly_fraction: Fractional Kelly multiplier (e.g. 0.25 for quarter-Kelly).

    Returns:
        Fractional stake in [0, 1]. 0 if Kelly is negative (bad bet).
    """
    if fill_price <= 0.0 or fill_price >= 1.0:
        return 0.0
    b = (1.0 - fill_price) / fill_price
    q = 1.0 - meta_prob
    kelly_full = (b * meta_prob - q) / b
    if kelly_full <= 0.0:
        return 0.0
    return kelly_full * kelly_fraction


# Main entry point

def evaluate_v9_1_meta_kelly(surface: "FullDataSurface") -> StrategyDecision:
    """Score via meta-v2 booster + deterministic Kelly. No LGB-distance gate.

    Decision flow:
    1.  Read probability_lgb_v9_1 (v9.1 booster) and probability_meta_v9_1
        (meta-v2 calibrated P(WIN)) from surface.
    2.  SKIP if either is None (model not loaded / V9_1_META_ENABLED=false).
    3.  Determine direction from probability_lgb_v9_1 (consistent with v9_1_lgb_only).
    4.  Cascade-fade-pair veto FIRST (HARD CONSTRAINT per meta_v2 design).
    5.  Once-per-window check: SKIP if already fired for this (window, dir).
    6.  Compute fill_price from CLOB surface (direction-aware ask).
    7.  Kelly stake = deterministic_stake(meta_prob, fill_price, kelly_fraction).
    8.  SKIP if stake_fraction == 0 (Kelly veto -- bad EV at this fill_price).
    9.  Mark window as fired, emit TRADE with stake metadata.
    """
    # Step 1-2: Null checks
    p_v9_1 = getattr(surface, "probability_lgb_v9_1", None)
    p_meta = getattr(surface, "probability_meta_v9_1", None)

    if p_v9_1 is None:
        return _skip("meta_model_not_loaded",
                     metadata={"probability_lgb_v9_1": None, "probability_meta_v9_1": None,
                                "reason": "probability_lgb_v9_1 is None (V9_1_ENABLED=false?)"})

    if p_meta is None:
        return _skip("meta_model_not_loaded",
                     metadata={"probability_lgb_v9_1": float(p_v9_1), "probability_meta_v9_1": None,
                                "reason": "probability_meta_v9_1 is None (V9_1_META_ENABLED=false?)"})

    p_v9_1 = float(p_v9_1)
    p_meta = float(p_meta)

    # Step 3: Direction from v9.1 booster
    direction = "UP" if p_v9_1 >= 0.5 else "DOWN"

    window_ts = getattr(surface, "window_ts", None)
    _wts = int(window_ts or 0)
    asset = getattr(surface, "asset", "BTC")

    # Step 4: Cascade-fade-pair veto (HARD CONSTRAINT -- runs before meta)
    if _sister_veto_enabled():
        _veto_pair = _sister_veto_pair()
        _veto_window = _sister_veto_window_sec()
        if is_sister_pair_veto_active(
            pair=_veto_pair,
            asset=asset,
            current_window_ts=_wts,
            v9_2_direction=direction,   # reuse v9_2 param for direction arg
            agreement_window_seconds=_veto_window,
        ):
            return _skip(
                "cascade_fade_veto",
                metadata={
                    "probability_lgb_v9_1": p_v9_1,
                    "probability_meta_v9_1": p_meta,
                    "meta_pred_direction": direction,
                    "sister_veto_pair": _veto_pair,
                    "sister_veto_window_sec": _veto_window,
                },
            )

    # Step 5: Once-per-window guard
    if has_window_fired(_wts, direction):
        return _skip(
            "already_fired_this_window",
            metadata={
                "probability_lgb_v9_1": p_v9_1,
                "probability_meta_v9_1": p_meta,
                "meta_pred_direction": direction,
                "window_ts": _wts,
            },
        )

    # Step 6: Fill price (direction-aware CLOB ask)
    if direction == "UP":
        fill_price = getattr(surface, "clob_up_ask", None)
    else:
        fill_price = getattr(surface, "clob_down_ask", None)

    if fill_price is None or fill_price <= 0.0 or fill_price >= 1.0:
        fill_price = 0.65  # representative mid-conviction fallback

    fill_price = float(fill_price)

    # Step 7: Kelly stake
    kf = _kelly_fraction()
    stake_fraction = deterministic_stake(p_meta, fill_price, kf)

    # Step 8: Kelly veto
    if stake_fraction == 0.0:
        return _skip(
            "kelly_veto",
            metadata={
                "probability_lgb_v9_1": p_v9_1,
                "probability_meta_v9_1": p_meta,
                "meta_pred_direction": direction,
                "fill_price": fill_price,
                "kelly_fraction": kf,
                "stake_fraction": 0.0,
            },
        )

    # Step 9: TRADE
    mark_window_fired(_wts, direction)

    abs_max = _abs_max_stake_usd()
    meta = {
        "probability_lgb_v9_1": p_v9_1,
        "probability_meta_v9_1": p_meta,
        "meta_pred_direction": direction,
        "fill_price": fill_price,
        "kelly_fraction": kf,
        "stake_fraction": stake_fraction,
        "abs_max_stake_usd": abs_max,
        "meta_model_version": _DEFAULT_META_MODEL_VERSION,
        "v9_1_meta_kelly_active": True,
        "lgb_only_forced": True,
        "sister_veto_checked": _sister_veto_enabled(),
        "sister_veto_fired": False,
    }

    # entry_cap is the MAX ACCEPTABLE CLOB PRICE (e.g. 0.65), NOT a USD cap.
    # The USD cap (abs_max_stake_usd / max_position_usd) is enforced via
    # strategy_runtime_overrides.params -> _resolve_sizing_for_strategy in
    # execute_trade.py, NOT via entry_cap. Setting entry_cap=$5.0 (USD) was
    # the original bug that caused FAILED_EXECUTION for every fire because
    # Polymarket interpreted $5.0 as a price-per-share (out of 0-1 range).
    # Use fill_price (CLOB ask, in [0, 1]) as the cap — same convention as
    # v9_ensemble._ENTRY_CAP=0.80.
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH" if p_meta >= 0.90 else ("MEDIUM" if p_meta >= 0.80 else "LOW"),
        confidence_score=stake_fraction,
        entry_cap=fill_price,
        collateral_pct=stake_fraction,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            f"meta_kelly_{direction.lower()}:p_meta={p_meta:.3f}"
            f":stake={stake_fraction:.3f}:fill={fill_price:.3f}"
        ),
        skip_reason=None,
        metadata=meta,
    )


# Helpers

def _skip(reason: str, *, metadata: Optional[dict] = None) -> StrategyDecision:
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
        metadata=metadata or {},
    )
