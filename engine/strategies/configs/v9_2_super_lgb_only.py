"""Hook for v9_2_super_lgb_only — Optuna-tuned v9.2 booster, non-consecutive
conviction gate, per-(vpin_regime × direction) cohort routing.

Architecture: sibling of v9_1_lgb_only (PR #466). Reads `probability_lgb_v9_2`
from the surface (emitted by timesfm-service when the v9.2-optuna booster is
loaded). Applies the non-consecutive ≥N-ticks-above-X cohort gate AFTER
direction is determined by v9_ensemble but BEFORE delta-alignment (mirrors
PR #494 order). Delegates the base gate stack to v9_ensemble.

New mechanic — non-consecutive N-of-M ticks:
    "N qualifying ticks anywhere in [t-300, t-60]" (not N consecutive ticks).
    State is stored in `_v9_2_qualifying_tick_count`, keyed by
    (strategy_id, window_ts, direction). Resets on window close
    (different direction or eval_offset out of band).

Per-(vpin_regime × direction) thresholds from V9_2_GATE_CONFIG.html:
    TRANSITION_UP/8, TRANSITION_DOWN/12, CASCADE_UP/8, CASCADE_DOWN/12,
    NORMAL_UP/8, NORMAL_DOWN/8, CALM/excluded.

Per-direction blocked_utc_hours reuse the PR #494 helpers already in
v9_ensemble:
    UP: [4, 9, 13, 19, 23], DOWN: [2, 9, 14, 15].

The cohort gate fires after direction is known but BEFORE delta-alignment
(same position as the per-direction blocked_utc_hours gate at step 9a in
v9_ensemble, making v9.2-super a clean superset). To achieve this, v9_2
runs the v9_ensemble path first (which halts at step 9 before the cohort
check), then applies the cohort gate, then returns.

NOTE: because we fully delegate to v9_ensemble for the base gates, and
v9_ensemble has no hook for "inject logic between step 9a and 9b", we
implement this differently:
    1. Run v9_ensemble with a patched surface (probability_lgb = v9_2 value).
    2. If the base gates TRADE, apply the cohort gate check as a post-filter.
    3. If cohort gate blocks, return SKIP with reason=cohort_below_threshold.

This ensures the non-consecutive gate never fires BEFORE the base gates have
passed — it's strictly a refinement on top. Any base-gate SKIP passes through
unchanged (so dashboards see the original skip reason, not a v9.2 override).

Sister-pair veto (hub notes #394 / #395 — ratified 2026-05-08):
    After the cohort gate qualifies a fire, the sister-pair veto checks
    whether BOTH v9_1_cascade_fade_late AND v9_cascade_fade_late fired
    OPPOSITE to v9.2 within ±20s of the current window_ts. If so, v9.2
    skips with reason=sister_pair_veto. Backtests show contrarian cases
    (sisters oppose v9.2) produce WR 64-70% vs consensus WR 86-89%,
    delta +16.4pp to +24.5pp — both above the 10pp ratify threshold
    with n≥30 in each bucket. Applies all-regimes.

    Gate order after this PR:
      cohort gate (≥N ticks ≥X conviction) — already existed
      block_cells (hour-of-day) — already existed
      sister_pair_veto — NEW (this PR)
      oracle_agreement_min_sources — already existed (via v9_ensemble)

If `probability_lgb_v9_2` is None on the surface, SKIP with
reason=`v9_2_model_not_loaded` (mirrors v9_1's pattern from PR #466).

Hub note #356 — PR-B handover.
Hub notes #394 / #395 — sister-pair veto ratification.
timesfm-service docs/V9_2_GATE_CONFIG.html — full gate spec + data.
"""
from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9
from strategies.sister_veto_bus import is_sister_pair_veto_active

_STRATEGY_ID = "v9_2_super_lgb_only"
_VERSION = "9.2.0-super-canary"

# ── Default cohort thresholds (lifted verbatim from V9_2_GATE_CONFIG.html) ──
# Keyed as "{vpin_regime}_{direction}".
_DEFAULT_CONVICTION_X: dict[str, Optional[float]] = {
    "TRANSITION_UP":   0.85,
    "TRANSITION_DOWN": 0.85,
    "CASCADE_UP":      0.85,
    "CASCADE_DOWN":    0.85,
    "NORMAL_UP":       0.85,
    "NORMAL_DOWN":     0.85,
    "CALM":            None,  # excluded — n<15 fires at every X
}

_DEFAULT_TICKS_N: dict[str, int] = {
    "TRANSITION_UP":   8,
    "TRANSITION_DOWN": 12,
    "CASCADE_UP":      8,
    "CASCADE_DOWN":    12,
    "NORMAL_UP":       8,
    "NORMAL_DOWN":     8,
}

# ── Non-consecutive qualifying-tick state machine ─────────────────────────
# Keyed by "{strategy_id}:{window_ts}:{direction}" → int count.
# Resets when direction changes OR window_ts changes (new window).
_v9_2_qualifying_tick_count: dict[str, int] = {}


def _cohort_key(window_ts: int, direction: str) -> str:
    return f"{_STRATEGY_ID}:{window_ts}:{direction}"


def count_qualifying_tick_v9_2(
    window_ts: int,
    direction: str,
    conviction: float,
    conviction_threshold: float,
) -> int:
    """Increment and return the qualifying-tick counter for this (window, direction).

    A tick "qualifies" when its conviction (max(p_up, 1−p_up)) ≥ X for the
    matching cohort. Unlike the consecutive-ticks gate, this accumulates
    non-consecutive qualifying ticks — any tick in the eval band counts once.

    State persists across calls within the same window's lifetime.
    Call reset_qualifying_ticks_v9_2() on window resolve / direction flip.

    Returns:
        The updated count of qualifying ticks for this (window_ts, direction).
    """
    key = _cohort_key(window_ts, direction)
    current = _v9_2_qualifying_tick_count.get(key, 0)
    if conviction >= conviction_threshold:
        current += 1
    _v9_2_qualifying_tick_count[key] = current
    return current


def get_qualifying_tick_count_v9_2(window_ts: int, direction: str) -> int:
    """Return current qualifying-tick count. Used by tests and diagnostics."""
    return _v9_2_qualifying_tick_count.get(_cohort_key(window_ts, direction), 0)


def reset_qualifying_ticks_v9_2(window_ts: int, direction: str) -> None:
    """Reset counter for a (window, direction) pair. Called on SKIP/window close."""
    _v9_2_qualifying_tick_count.pop(_cohort_key(window_ts, direction), None)


def reset_all_qualifying_ticks_v9_2() -> None:
    """Clear all state. Used by tests."""
    _v9_2_qualifying_tick_count.clear()


# ── Config knobs (read from gate_params or env) ───────────────────────────

def _conviction_x_for_cohort(cohort_key: str) -> Optional[float]:
    """Return the conviction threshold X for this cohort, or None if excluded."""
    thresholds = _gp.get_list("conviction_x_thresholds", [])
    # YAML delivers this as a dict when used as a gate_params block; but
    # get_list may coerce it. Prefer direct lookup from active params.
    params = _gp._ACTIVE.get()
    raw = params.get("conviction_x_thresholds", {})
    if isinstance(raw, dict) and cohort_key in raw:
        val = raw[cohort_key]
        return float(val) if val is not None else None
    return _DEFAULT_CONVICTION_X.get(cohort_key)


def _ticks_n_for_cohort(cohort_key: str) -> int:
    """Return the qualifying-tick threshold N for this cohort."""
    params = _gp._ACTIVE.get()
    raw = params.get("ticks_n_thresholds", {})
    if isinstance(raw, dict) and cohort_key in raw:
        return int(raw[cohort_key])
    return _DEFAULT_TICKS_N.get(cohort_key, 8)


def _blocked_utc_hours_up_v9_2() -> list[int]:
    return _gp.get_int_list("blocked_utc_hours_up", "V9_2_BLOCKED_HOURS_UP", [4, 9, 13, 19, 23])


def _blocked_utc_hours_dn_v9_2() -> list[int]:
    return _gp.get_int_list("blocked_utc_hours_down", "V9_2_BLOCKED_HOURS_DN", [2, 9, 14, 15])


# ── Sister-pair veto config ───────────────────────────────────────────────
# Defaults match the YAML sister_pair_veto block. Config is read live from
# gate_params so Billy can tune via strategy_runtime_overrides without restart.

_SISTER_VETO_DEFAULT_PAIR = ["v9_1_cascade_fade_late", "v9_cascade_fade_late"]
_SISTER_VETO_DEFAULT_WINDOW_SEC = 20


def _sister_veto_enabled() -> bool:
    """Return True if the sister-pair veto is active (default: True)."""
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", True))
    # Flat-params style (strategy_runtime_overrides JSON):
    return bool(params.get("sister_veto_enabled", True))


def _sister_veto_pair() -> list[str]:
    """Return the pair of sister strategy IDs to check."""
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict) and "pair" in cfg:
        v = cfg["pair"]
        return list(v) if isinstance(v, (list, tuple)) else _SISTER_VETO_DEFAULT_PAIR
    # Flat-params style:
    v = params.get("sister_veto_pair")
    if isinstance(v, (list, tuple)):
        return list(v)
    return _SISTER_VETO_DEFAULT_PAIR


def _sister_veto_window_sec() -> int:
    """Return the agreement window in seconds (default: 20)."""
    params = _gp._ACTIVE.get()
    cfg = params.get("sister_pair_veto", {})
    if isinstance(cfg, dict) and "agreement_window_seconds" in cfg:
        return int(cfg["agreement_window_seconds"])
    # Flat-params style:
    v = params.get("sister_veto_agreement_window_seconds")
    if v is not None:
        return int(v)
    return _SISTER_VETO_DEFAULT_WINDOW_SEC


# ── Main entry point ──────────────────────────────────────────────────────

def evaluate_v9_2_super_lgb_only(surface: "FullDataSurface") -> StrategyDecision:
    """Score with v9.2-optuna booster + non-consecutive cohort gate + sister-pair veto.

    Surface flow:
    1.  Read `probability_lgb_v9_2` from surface.
    2.  SKIP if None (model not loaded).
    3.  Compute conviction = max(p, 1−p) and predicted direction.
    4.  Determine cohort = "{vpin_regime}_{direction}".
    5.  SKIP if cohort is CALM (excluded).
    6.  Check per-direction blocked_utc_hours (UP and DOWN separately).
    7.  Increment qualifying-tick counter for this (window_ts, direction, conviction ≥ X).
    8.  Swap probability_lgb_v9_2 → probability_lgb slot; delegate to v9_ensemble.
    9.  If base gates SKIP, reset qualifying counter; return SKIP.
    10. If base gates TRADE, apply cohort gate: count ≥ N → TRADE; else SKIP.
    11. Sister-pair veto (NEW — hub notes #394 / #395): if BOTH
        v9_1_cascade_fade_late AND v9_cascade_fade_late fired OPPOSITE within
        ±agreement_window_seconds → SKIP sister_pair_veto.
    12. Stamp v9.2 metadata and relabel strategy identity.
    """
    p_v9_2 = getattr(surface, "probability_lgb_v9_2", None)
    if p_v9_2 is None:
        return _skip_v9_2(
            "v9_2_model_not_loaded",
            metadata={"probability_lgb_v9_2": None, "v9_2_enabled": False},
        )

    p_v9_2 = float(p_v9_2)
    conviction = max(p_v9_2, 1.0 - p_v9_2)
    pred_direction = "UP" if p_v9_2 >= 0.5 else "DOWN"

    # ── Determine VPIN regime cohort ──────────────────────────────────────
    vpin_regime = getattr(surface, "regime", None) or "UNKNOWN"
    cohort_key = f"{vpin_regime}_{pred_direction}"

    # ── CALM exclusion ────────────────────────────────────────────────────
    if vpin_regime == "CALM":
        return _skip_v9_2(
            "cohort_excluded_calm",
            metadata={
                "probability_lgb_v9_2": p_v9_2,
                "v9_2_conviction": conviction,
                "v9_2_pred_direction": pred_direction,
                "v9_2_cohort": cohort_key,
                "v9_2_gate_fired": False,
            },
        )

    # ── Per-direction blocked_utc_hours ───────────────────────────────────
    # Check BEFORE the qualifying-tick counter so we don't accumulate
    # ticks in blocked hours (same philosophy as v9_ensemble step 9a).
    window_ts = getattr(surface, "window_ts", None)
    hour_utc = _get_utc_hour(window_ts)
    if hour_utc is not None:
        blocked_up = set(_blocked_utc_hours_up_v9_2())
        blocked_dn = set(_blocked_utc_hours_dn_v9_2())
        if pred_direction == "UP" and hour_utc in blocked_up:
            reset_qualifying_ticks_v9_2(int(window_ts or 0), pred_direction)
            return _skip_v9_2(
                f"blocked_utc_hour_up: hour={hour_utc}",
                metadata={
                    "probability_lgb_v9_2": p_v9_2,
                    "v9_2_conviction": conviction,
                    "v9_2_pred_direction": pred_direction,
                    "v9_2_cohort": cohort_key,
                    "v9_2_gate_fired": False,
                    "blocked_hour": hour_utc,
                },
            )
        if pred_direction == "DOWN" and hour_utc in blocked_dn:
            reset_qualifying_ticks_v9_2(int(window_ts or 0), pred_direction)
            return _skip_v9_2(
                f"blocked_utc_hour_down: hour={hour_utc}",
                metadata={
                    "probability_lgb_v9_2": p_v9_2,
                    "v9_2_conviction": conviction,
                    "v9_2_pred_direction": pred_direction,
                    "v9_2_cohort": cohort_key,
                    "v9_2_gate_fired": False,
                    "blocked_hour": hour_utc,
                },
            )

    # ── Non-consecutive qualifying-tick accumulation ───────────────────────
    conviction_x = _conviction_x_for_cohort(cohort_key)
    if conviction_x is None:
        # Unknown/excluded cohort — fail safe
        return _skip_v9_2(
            f"cohort_excluded: {cohort_key}",
            metadata={
                "probability_lgb_v9_2": p_v9_2,
                "v9_2_conviction": conviction,
                "v9_2_pred_direction": pred_direction,
                "v9_2_cohort": cohort_key,
                "v9_2_gate_fired": False,
            },
        )

    ticks_n = _ticks_n_for_cohort(cohort_key)
    _wts = int(window_ts or 0)
    tick_count = count_qualifying_tick_v9_2(
        _wts, pred_direction, conviction, conviction_x
    )

    # ── Delegate to v9_ensemble base gate stack ───────────────────────────
    # Swap v9.2 prediction onto probability_lgb slot; disable classifier.
    _orig_lgb = getattr(surface, "probability_lgb", None)
    _orig_pc = getattr(surface, "probability_classifier", None)
    _orig_regime = getattr(surface, "v4_regime", None)

    object.__setattr__(surface, "probability_lgb", p_v9_2)
    object.__setattr__(surface, "probability_classifier", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "chop")

    try:
        base_decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    # ── Base gate SKIP → pass through, reset tick counter ─────────────────
    if base_decision.action != "TRADE":
        reset_qualifying_ticks_v9_2(_wts, pred_direction)
        meta = dict(base_decision.metadata or {})
        meta.update({
            "probability_lgb_v9_2": p_v9_2,
            "probability_lgb_prod": _orig_lgb,
            "v9_2_conviction": conviction,
            "v9_2_pred_direction": pred_direction,
            "v9_2_cohort": cohort_key,
            "v9_2_gate_fired": False,
            "v9_2_tick_count": tick_count,
            "v9_2_tick_threshold": ticks_n,
            "lgb_only_forced": True,
            "v9_2_active": True,
        })
        return StrategyDecision(
            action=base_decision.action,
            direction=base_decision.direction,
            confidence=base_decision.confidence,
            confidence_score=base_decision.confidence_score,
            entry_cap=base_decision.entry_cap,
            collateral_pct=base_decision.collateral_pct,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason=base_decision.entry_reason or "",
            skip_reason=base_decision.skip_reason,
            metadata=meta,
        )

    # ── Cohort gate: ≥N qualifying ticks? ────────────────────────────────
    if tick_count < ticks_n:
        # Not yet enough qualifying ticks — keep accumulating; SKIP this tick.
        return _skip_v9_2(
            "cohort_below_threshold",
            metadata={
                "probability_lgb_v9_2": p_v9_2,
                "probability_lgb_prod": _orig_lgb,
                "v9_2_conviction": conviction,
                "v9_2_pred_direction": pred_direction,
                "v9_2_cohort": cohort_key,
                "v9_2_gate_fired": False,
                "v9_2_tick_count": tick_count,
                "v9_2_tick_threshold": ticks_n,
                "v9_2_conviction_x": conviction_x,
                "vpin_regime": vpin_regime,
                "lgb_only_forced": True,
                "v9_2_active": True,
            },
        )

    # -- Cohort gate FIRED -- check sister-pair veto BEFORE committing ------
    # Reset counter so it doesn't bleed into the next window/opportunity.
    reset_qualifying_ticks_v9_2(_wts, pred_direction)

    # -- Sister-pair veto (hub notes #394 / #395) --------------------------
    # If BOTH cascade-fade sisters fired OPPOSITE to v9.2 within the
    # agreement window, skip. Applies all-regimes (backtests validated).
    # Enabled by default; disable via gate_params sister_pair_veto.enabled=false.
    if _sister_veto_enabled():
        _veto_pair = _sister_veto_pair()
        _veto_window = _sister_veto_window_sec()
        _asset = getattr(surface, "asset", "BTC")
        if is_sister_pair_veto_active(
            pair=_veto_pair,
            asset=_asset,
            current_window_ts=_wts,
            v9_2_direction=pred_direction,
            agreement_window_seconds=_veto_window,
        ):
            return _skip_v9_2(
                "sister_pair_veto",
                metadata={
                    "probability_lgb_v9_2": p_v9_2,
                    "probability_lgb_prod": _orig_lgb,
                    "v9_2_conviction": conviction,
                    "v9_2_pred_direction": pred_direction,
                    "v9_2_cohort": cohort_key,
                    "v9_2_gate_fired": False,
                    "v9_2_tick_count": tick_count,
                    "v9_2_tick_threshold": ticks_n,
                    "v9_2_conviction_x": conviction_x,
                    "vpin_regime": vpin_regime,
                    "lgb_only_forced": True,
                    "v9_2_active": True,
                    "sister_veto_pair": _veto_pair,
                    "sister_veto_window_sec": _veto_window,
                },
            )

    meta = dict(base_decision.metadata or {})
    meta.update({
        "probability_lgb_v9_2": p_v9_2,
        "probability_lgb_prod": _orig_lgb,
        "v9_2_conviction": conviction,
        "v9_2_pred_direction": pred_direction,
        "v9_2_cohort": cohort_key,
        "v9_2_gate_fired": True,
        "v9_2_tick_count": tick_count,
        "v9_2_tick_threshold": ticks_n,
        "v9_2_conviction_x": conviction_x,
        "vpin_regime": vpin_regime,
        "lgb_only_forced": True,
        "v9_2_active": True,
        "sister_veto_checked": True,
        "sister_veto_fired": False,
    })

    direction = base_decision.direction
    entry_reason = (
        base_decision.entry_reason
        .replace("v9_ensemble", _STRATEGY_ID)
        .replace("v9_lgb_only", _STRATEGY_ID)
        if base_decision.entry_reason
        else ""
    )

    return StrategyDecision(
        action=base_decision.action,
        direction=direction,
        confidence=base_decision.confidence,
        confidence_score=base_decision.confidence_score,
        entry_cap=base_decision.entry_cap,
        collateral_pct=base_decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=entry_reason,
        skip_reason=None,
        metadata=meta,
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _skip_v9_2(reason: str, *, metadata: Optional[dict] = None) -> StrategyDecision:
    """Emit a SKIP decision branded as v9_2_super_lgb_only."""
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


def _get_utc_hour(window_ts: Optional[int]) -> Optional[int]:
    """Parse UTC hour from window_ts (Unix epoch seconds). Returns None on error."""
    if window_ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(int(window_ts), _dt.timezone.utc).hour
    except (TypeError, ValueError, OSError):
        return None
