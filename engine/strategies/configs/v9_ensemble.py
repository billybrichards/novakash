"""Hook for v9_ensemble strategy — Reinforced-Agreement LGB + v2-classifier.

Design: layered on top of v8_champion_lgb_only. Delegates gate helpers and
cooldown machinery to the v8_lgb_only module; adds ensemble-specific gates
(disagreement veto, direction-agreement, T-minus weighted blend, VHC
reinforcement with bypass logic, hard LGB safety floor).

When `surface.probability_classifier is None` (classifier box warming up or
shadow-read wiring not yet deployed in the engine), v9 falls back to the
v8_lgb_only gate logic and logs `fallback_reason=pc_null` in metadata.

Gate order (TRADE path):
   1. Timing                 (shared — v8 helper)
   2. Hour block             (shared — v8 helper)
   3. Regime (v4)            (shared — v8 helper)
   4. Source agreement       (shared — v8 helper)
   5. VPIN guard             (shared — v8 helper)
   6. PC availability        (R1 — fallback to v8_lgb_only on pc=None)
   7. Disagreement veto      (R2)
   8. Direction agreement    (R3)
   9. T-minus blend          (R4 — compute pu, direction)
  10. Hard LGB safety floor  (R6 — bypassable by VHC for UP dist)
  11. TRANSITION regime      (R5 — bypassable by VHC)
  12. Oracle direction       (shared)
  13. Fill band              (R7 — shared)
  14. UP / DOWN fill floors  (R8 — shared, NOT bypassed by VHC)
  15. Post-loss cooldown     (R15 — shared, NOT bypassed by VHC)

Conviction tiers computed post-gates — see _classify_conviction below.

See Hub notes #221 (strategy plan), #222 (v8_lgb_only template), and
#226 (classifier handoff / pc stats) for the full spec and evidence.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

# Reuse shared v8 helpers and cooldown state machine.
from strategies.configs.v8_champion import (
    _gate,
    _min_offset_sec,
    _max_offset_sec,
    _tradeable_regimes,
    _blocked_utc_hours,
    _require_chainlink,
    _require_tiingo,
    _skip_on_oracle_disagree,
    _vpin_min,
    _vpin_max,
    _up_min_fill_price,
    _fill_band_max,
)
from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only,
    _fill_band_min_lgb,
    _down_min_fill_price,
    _lgb_dist_min_down,
    _lgb_dist_min_up,
    _block_down_vpin_regimes,
    _block_up_vpin_regimes,
    _in_cooldown,
    record_loss,
    reset_cooldown,
)

_STRATEGY_ID = "v9_ensemble"
_VERSION = "9.0.0"
_ENTRY_CAP = 0.80


# ── v9-specific config knobs ───────────────────────────────────────────────
def _ensemble_disagreement_threshold() -> float:
    return _gp.get_float(
        "ensemble_disagreement_threshold",
        "V9_ENSEMBLE_DISAGREEMENT_THRESHOLD",
        0.25,
    )


def _require_direction_agreement() -> bool:
    return _gp.get_bool(
        "require_direction_agreement",
        "V9_REQUIRE_DIRECTION_AGREEMENT",
        True,
    )


def _pc_weight_t_60() -> float:
    return _gp.get_float("pc_weight_t_60", "V9_PC_WEIGHT_T_60", 0.55)


def _pc_weight_t_120() -> float:
    return _gp.get_float("pc_weight_t_120", "V9_PC_WEIGHT_T_120", 0.50)


def _pc_weight_t_180() -> float:
    return _gp.get_float("pc_weight_t_180", "V9_PC_WEIGHT_T_180", 0.45)


def _pc_weight_t_200() -> float:
    return _gp.get_float("pc_weight_t_200", "V9_PC_WEIGHT_T_200", 0.35)


def _vhc_threshold() -> float:
    return _gp.get_float("vhc_threshold", "V9_VHC_THRESHOLD", 0.25)


def _vhc_bypass_transition() -> bool:
    return _gp.get_bool(
        "vhc_bypass_transition", "V9_VHC_BYPASS_TRANSITION", True
    )


def _vhc_bypass_up_dist() -> bool:
    return _gp.get_bool(
        "vhc_bypass_up_dist", "V9_VHC_BYPASS_UP_DIST", True
    )


def _vhc_kelly_multiplier() -> float:
    return _gp.get_float(
        "vhc_kelly_multiplier", "V9_VHC_KELLY_MULTIPLIER", 2.0
    )


def _conviction_high_dist() -> float:
    return _gp.get_float(
        "conviction_high_dist", "V9_CONVICTION_HIGH_DIST", 0.20
    )


def _conviction_medium_dist() -> float:
    return _gp.get_float(
        "conviction_medium_dist", "V9_CONVICTION_MEDIUM_DIST", 0.12
    )


def _conviction_low_dist() -> float:
    return _gp.get_float(
        "conviction_low_dist", "V9_CONVICTION_LOW_DIST", 0.05
    )


def _fallback_to_lgb_on_pc_null() -> bool:
    return _gp.get_bool(
        "fallback_to_lgb_on_pc_null",
        "V9_FALLBACK_TO_LGB_ON_PC_NULL",
        True,
    )


# ── Delta alignment gate (2026-04-24, note #301) ──────────────────────────
def _delta_gate_enabled() -> bool:
    return _gp.get_bool(
        "delta_gate_enabled", "V9_DELTA_GATE_ENABLED", True
    )


def _min_alignment_bps() -> float:
    return _gp.get_float(
        "min_alignment_bps", "V9_MIN_ALIGNMENT_BPS", 1.0
    )


# ── 3-tick entry confirmation (2026-04-24, note #298) ─────────────────────
def _min_consecutive_pass_ticks() -> int:
    return _gp.get_int(
        "min_consecutive_pass_ticks",
        "V9_MIN_CONSECUTIVE_PASS_TICKS",
        3,
    )


# ── Post-fill exit monitoring params (2026-04-24, note #299) ──────────────
def _exit_monitor_enabled() -> bool:
    return _gp.get_bool(
        "exit_monitor_enabled", "V9_EXIT_MONITOR_ENABLED", True
    )


def _exit_shadow_mode() -> bool:
    return _gp.get_bool(
        "exit_shadow_mode", "V9_EXIT_SHADOW_MODE", True
    )


def _exit_min_hold_seconds() -> int:
    return _gp.get_int(
        "exit_min_hold_seconds", "V9_EXIT_MIN_HOLD_SECONDS", 10
    )


def _exit_no_exit_last_seconds() -> int:
    return _gp.get_int(
        "exit_no_exit_last_seconds", "V9_EXIT_NO_EXIT_LAST_SECONDS", 30
    )


def _exit_consecutive_flip_ticks() -> int:
    return _gp.get_int(
        "exit_consecutive_flip_ticks",
        "V9_EXIT_CONSECUTIVE_FLIP_TICKS",
        3,
    )


def _exit_lgb_flip_enabled() -> bool:
    return _gp.get_bool(
        "exit_lgb_flip_enabled", "V9_EXIT_LGB_FLIP_ENABLED", True
    )


def _exit_oracle_flip_enabled() -> bool:
    return _gp.get_bool(
        "exit_oracle_flip_enabled", "V9_EXIT_ORACLE_FLIP_ENABLED", True
    )


def _exit_max_retries() -> int:
    return _gp.get_int(
        "exit_max_retries", "V9_EXIT_MAX_RETRIES", 1
    )


def _exit_retry_timeout_seconds() -> int:
    return _gp.get_int(
        "exit_retry_timeout_seconds",
        "V9_EXIT_RETRY_TIMEOUT_SECONDS",
        5,
    )


# ── TRANSITION strong-oracle bypass (2026-04-24, note #228) ────────────────
# Independent bypass for the TRANSITION block (R5). Fires when chainlink +
# tiingo BOTH agree direction AND avg(|Δcl|,|Δti|) >= threshold AND LGB
# conviction is strong. Binance excluded by design. Runs alongside the
# existing VHC bypass — either path may unblock the regime gate.
def _transition_strong_bypass_enabled() -> bool:
    return _gp.get_bool(
        "transition_strong_bypass_enabled",
        "V9_TRANSITION_STRONG_BYPASS_ENABLED",
        True,
    )


def _transition_bypass_min_avg_pct_delta() -> float:
    """Threshold expressed in PERCENT (0.05 = 0.05%).

    Surface deltas are FRACTIONAL (e.g. -0.0007 = -0.07%). We divide by 100
    at comparison time.
    """
    return _gp.get_float(
        "transition_bypass_min_avg_pct_delta",
        "V9_TRANSITION_BYPASS_MIN_AVG_PCT_DELTA",
        0.05,
    )


def _transition_bypass_min_lgb_dist() -> float:
    return _gp.get_float(
        "transition_bypass_min_lgb_dist",
        "V9_TRANSITION_BYPASS_MIN_LGB_DIST",
        0.20,
    )


def _should_bypass_transition(
    direction: str,
    pl: float,
    delta_chainlink: Optional[float],
    delta_tiingo: Optional[float],
) -> tuple[bool, str]:
    """Return (bypass, diag_string). See note #228 for rule."""
    if not _transition_strong_bypass_enabled():
        return (False, "bypass_disabled")
    if delta_chainlink is None or delta_tiingo is None:
        return (False, "oracle_missing")
    # Surface deltas are fractional; threshold param is in percent.
    avg_mag_frac = (abs(delta_chainlink) + abs(delta_tiingo)) / 2.0
    min_avg_frac = _transition_bypass_min_avg_pct_delta() / 100.0
    sign_agree = (
        (direction == "UP" and delta_chainlink > 0 and delta_tiingo > 0)
        or (direction == "DOWN" and delta_chainlink < 0 and delta_tiingo < 0)
    )
    pl_dist = abs(pl - 0.5)
    lgb_strong = pl_dist >= _transition_bypass_min_lgb_dist()
    if avg_mag_frac >= min_avg_frac and sign_agree and lgb_strong:
        return (
            True,
            f"avg_mag={avg_mag_frac * 100:.4f}% pl_dist={pl_dist:.3f}",
        )
    return (
        False,
        f"avg_mag={avg_mag_frac * 100:.4f}% sign_agree={sign_agree} "
        f"lgb_strong={lgb_strong}",
    )


# ── 3-tick entry confirmation state machine (independent from v8) ─────────
_v9_consecutive_pass: dict[str, tuple[int, str]] = {}


def check_confirmation_v9(strategy_id: str, window_ts: int, direction: str) -> bool:
    """Check if we have enough consecutive passing ticks for v9."""
    required = _min_consecutive_pass_ticks()
    if required <= 0:
        return True

    key = f"{strategy_id}:{window_ts}"
    prev = _v9_consecutive_pass.get(key)
    if prev and prev[1] == direction:
        count = prev[0] + 1
    else:
        count = 1
    _v9_consecutive_pass[key] = (count, direction)
    return count >= required


def get_confirmation_count_v9(strategy_id: str, window_ts: int) -> int:
    """Return the current consecutive pass count for v9. Used by tests."""
    key = f"{strategy_id}:{window_ts}"
    prev = _v9_consecutive_pass.get(key)
    return prev[0] if prev else 0


def reset_confirmation_v9(strategy_id: str, window_ts: int) -> None:
    """Reset v9 confirmation counter on SKIP."""
    key = f"{strategy_id}:{window_ts}"
    _v9_consecutive_pass.pop(key, None)


def reset_all_confirmations_v9() -> None:
    """Clear all v9 confirmation state. Used by tests."""
    _v9_consecutive_pass.clear()


# ── Helpers ────────────────────────────────────────────────────────────────
def _pc_weight_for_offset(offset: int) -> float:
    """Return the classifier weight based on T-minus offset bracket.

    Classifier has a freshness advantage near T-0 (TimesFM surface is
    fresher), degrades as the window ages. See Hub note #226 for the
    latency/freshness discussion.
    """
    if offset <= 60:
        return _pc_weight_t_60()
    if offset <= 120:
        return _pc_weight_t_120()
    if offset <= 180:
        return _pc_weight_t_180()
    return _pc_weight_t_200()


def _skip_v9(
    reason: str,
    gates: list[dict],
    *,
    direction: Optional[str] = None,
    extras: Optional[dict] = None,
) -> StrategyDecision:
    """Emit SKIP decision with v9 branding + metadata."""
    meta: dict = {"gate_results": gates}
    if direction is not None:
        meta["poly_direction"] = direction
    if extras:
        meta.update(extras)
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=None,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="",
        skip_reason=reason,
        metadata=meta,
    )


def _classify_conviction(
    pu: float,
    pc: float,
    pc_dir: str,
    pl_dir: str,
) -> tuple[str, float, bool]:
    """Return (label, score, is_vhc).

    score feeds the clob_sizing schedule — thresholds 0.55/0.45/0.35/0.25/0.0
    with labels vhc_reinforced/high/medium/low/skip.

    VHC (VERY_HIGH): |pc - 0.5| >= vhc_threshold AND pc direction == pl
    direction. Returns score >= 0.55 so the sizing schedule's top row
    (`vhc_reinforced`, modifier 2.5x or vhc_kelly_multiplier) triggers.
    """
    pc_dist = abs(pc - 0.5)
    pu_dist = abs(pu - 0.5)
    vhc_thr = _vhc_threshold()

    is_vhc = pc_dist >= vhc_thr and pc_dir == pl_dir
    if is_vhc:
        # Score above 0.55 maps to vhc_reinforced row (modifier set by YAML /
        # vhc_kelly_multiplier).
        return ("VERY_HIGH", 0.60, True)

    if pu_dist >= _conviction_high_dist() or pc_dist >= _conviction_high_dist():
        return ("HIGH", 0.50, False)
    if pu_dist >= _conviction_medium_dist():
        return ("MEDIUM", 0.40, False)
    if pu_dist >= _conviction_low_dist():
        return ("LOW", 0.30, False)
    # Shouldn't reach — earlier gates (R6 hard LGB floor) should have caught
    # low-conviction cases. Keep for defensive completeness.
    return ("LOW", 0.25, False)


# ── Hook entry ─────────────────────────────────────────────────────────────
def evaluate_v9_ensemble(surface: "FullDataSurface") -> StrategyDecision:
    """Main evaluation hook for v9_ensemble."""
    gates: list[dict] = []
    now = int(_time.time())

    # ── 1. Timing ──────────────────────────────────────────────────────────
    offset = surface.eval_offset or 0
    min_off = _min_offset_sec()
    max_off = _max_offset_sec()
    if offset < min_off or offset > max_off:
        gates.append(
            _gate(
                "timing",
                False,
                f"eval_offset={offset} outside [{min_off},{max_off}]",
            )
        )
        return _skip_v9(
            f"timing: eval_offset={offset} outside [{min_off},{max_off}]",
            gates,
        )
    gates.append(
        _gate("timing", True, f"eval_offset={offset} in [{min_off},{max_off}]")
    )

    # ── 2. Hour block ──────────────────────────────────────────────────────
    blocked = set(_blocked_utc_hours())
    if blocked:
        window_ts = getattr(surface, "window_ts", None)
        if window_ts:
            hour = _dt.datetime.fromtimestamp(
                int(window_ts), _dt.timezone.utc
            ).hour
            if hour in blocked:
                gates.append(
                    _gate(
                        "utc_hour_block",
                        False,
                        f"hour={hour} in blocked {sorted(blocked)}",
                    )
                )
                return _skip_v9(f"utc_hour_block: hour={hour}", gates)
            gates.append(
                _gate("utc_hour_block", True, f"hour={hour} allowed")
            )

    # ── 3. Regime (v4) ─────────────────────────────────────────────────────
    v4_regime = surface.v4_regime
    tradeable = _tradeable_regimes()
    if v4_regime is None:
        gates.append(_gate("v4_regime", False, "regime=None (unknown)"))
        return _skip_v9("v4_regime: regime unknown", gates)
    if v4_regime not in tradeable:
        gates.append(
            _gate(
                "v4_regime",
                False,
                f"regime={v4_regime} not in {sorted(tradeable)}",
            )
        )
        return _skip_v9(f"v4_regime={v4_regime} not tradeable", gates)
    gates.append(_gate("v4_regime", True, f"regime={v4_regime} tradeable"))

    # ── 4. Source agreement ────────────────────────────────────────────────
    missing: list[str] = []
    if _require_chainlink() and surface.delta_chainlink is None:
        missing.append("chainlink")
    if _require_tiingo() and surface.delta_tiingo is None:
        missing.append("tiingo")
    if missing:
        gates.append(
            _gate(
                "source_agreement",
                False,
                f"sources_missing: {','.join(missing)}",
            )
        )
        return _skip_v9(
            f"source_agreement: {','.join(missing)} missing", gates
        )
    gates.append(
        _gate("source_agreement", True, "chainlink + tiingo present")
    )

    # ── 5. VPIN guard ──────────────────────────────────────────────────────
    vpin = surface.vpin
    vmin = _vpin_min()
    vmax = _vpin_max()
    if vpin is None:
        gates.append(_gate("vpin", False, "vpin=None"))
        return _skip_v9("vpin: value unavailable", gates)
    if vpin < vmin or vpin > vmax:
        gates.append(
            _gate(
                "vpin",
                False,
                f"vpin={vpin:.3f} outside [{vmin:.2f},{vmax:.2f}]",
            )
        )
        return _skip_v9(
            f"vpin={vpin:.3f} outside [{vmin:.2f},{vmax:.2f}]", gates
        )
    gates.append(
        _gate("vpin", True, f"vpin={vpin:.3f} in [{vmin:.2f},{vmax:.2f}]")
    )

    # ── 6. PC availability / fallback (R1) ────────────────────────────────
    pc = getattr(surface, "probability_classifier", None)
    pl = getattr(surface, "probability_lgb", None)
    if pl is None:
        gates.append(_gate("pl_availability", False, "probability_lgb=None"))
        return _skip_v9("pl_availability: probability_lgb unavailable", gates)

    if pc is None and _fallback_to_lgb_on_pc_null():
        gates.append(
            _gate(
                "pc_availability",
                False,
                "probability_classifier=None; fallback to v8_lgb_only",
            )
        )
        # Delegate to v8_lgb_only. Its decision is returned as-is so the
        # shadow log shows v9 ran but the decision is the LGB-only one.
        fallback_decision = evaluate_v8_champion_lgb_only(surface)
        # Overlay v9 identity + fallback flag on the returned decision so
        # the hub/shadow log knows which strategy emitted the row.
        fallback_meta = dict(fallback_decision.metadata or {})
        fallback_meta["fallback_reason"] = "pc_null"
        fallback_meta["delegated_to"] = "v8_champion_lgb_only"
        # Also append our v9 gates log for traceability.
        existing_gates = fallback_meta.get("gate_results", [])
        fallback_meta["gate_results"] = gates + list(existing_gates)
        return StrategyDecision(
            action=fallback_decision.action,
            direction=fallback_decision.direction,
            confidence=fallback_decision.confidence,
            confidence_score=fallback_decision.confidence_score,
            entry_cap=fallback_decision.entry_cap,
            collateral_pct=fallback_decision.collateral_pct,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason=(
                f"v9_ensemble_fallback_lgb:"
                f"{fallback_decision.entry_reason}"
                if fallback_decision.entry_reason
                else ""
            ),
            skip_reason=fallback_decision.skip_reason,
            metadata=fallback_meta,
        )
    gates.append(
        _gate("pc_availability", True, f"pc={pc:.3f} present")
    )

    # ── 7. Disagreement veto (R2) ──────────────────────────────────────────
    disagree_thr = _ensemble_disagreement_threshold()
    disagreement = abs(pc - pl)
    if disagreement > disagree_thr:
        gates.append(
            _gate(
                "ensemble_disagreement",
                False,
                f"|pc-pl|={disagreement:.3f} > {disagree_thr:.2f}",
            )
        )
        return _skip_v9(
            f"ensemble_disagreement: diff={disagreement:.3f}",
            gates,
        )
    gates.append(
        _gate(
            "ensemble_disagreement",
            True,
            f"|pc-pl|={disagreement:.3f} <= {disagree_thr:.2f}",
        )
    )

    # ── 8. Direction agreement (R3) ────────────────────────────────────────
    pc_dir = "UP" if pc > 0.5 else "DOWN"
    pl_dir = "UP" if pl > 0.5 else "DOWN"
    if _require_direction_agreement() and pc_dir != pl_dir:
        gates.append(
            _gate(
                "direction_agreement",
                False,
                f"pc_dir={pc_dir} pl_dir={pl_dir} disagree",
            )
        )
        return _skip_v9(
            f"direction_agreement: pc={pc_dir} pl={pl_dir}", gates
        )
    gates.append(
        _gate("direction_agreement", True, f"both {pc_dir}")
    )

    # ── 9. T-minus blend (R4) ──────────────────────────────────────────────
    pc_weight = _pc_weight_for_offset(offset)
    pl_weight = 1.0 - pc_weight
    pu = pc_weight * pc + pl_weight * pl
    direction = "UP" if pu > 0.5 else "DOWN"
    gates.append(
        _gate(
            "ensemble_blend",
            True,
            f"pu={pu:.3f} "
            f"(pc={pc:.3f}*{pc_weight:.2f} + pl={pl:.3f}*{pl_weight:.2f}) "
            f"direction={direction}",
        )
    )

    # ── 9b. Delta alignment gate (AFTER direction known, BEFORE safety floor)
    # Skip if chainlink delta is moving AGAINST bet direction by more than
    # min_alignment_bps. See Hub note #301 / audit task #301.
    if _delta_gate_enabled() and surface.delta_chainlink is not None:
        alignment_bps = surface.delta_chainlink * 10000  # fraction to bps
        min_bps = _min_alignment_bps()
        if direction == "UP" and alignment_bps < -min_bps:
            gates.append(
                _gate(
                    "delta_gate",
                    False,
                    f"chainlink {alignment_bps:.1f}bp against UP "
                    f"(threshold: -{min_bps:.1f}bp)",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"delta_gate: chainlink {alignment_bps:.1f}bp against UP",
                gates,
                direction=direction,
            )
        if direction == "DOWN" and alignment_bps > min_bps:
            gates.append(
                _gate(
                    "delta_gate",
                    False,
                    f"chainlink +{alignment_bps:.1f}bp against DOWN "
                    f"(threshold: +{min_bps:.1f}bp)",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"delta_gate: chainlink +{alignment_bps:.1f}bp against DOWN",
                gates,
                direction=direction,
            )
        gates.append(
            _gate(
                "delta_gate",
                True,
                f"chainlink {alignment_bps:.1f}bp aligned with {direction}",
            )
        )
    elif _delta_gate_enabled():
        gates.append(
            _gate("delta_gate", True, "chainlink delta unavailable, skip gate")
        )

    # ── Determine VHC reinforcement early — gates below use it for bypass ─
    pc_dist = abs(pc - 0.5)
    is_vhc = pc_dist >= _vhc_threshold() and pc_dir == pl_dir
    vhc_bypasses: list[str] = []

    # ── 10. Hard LGB safety floor (R6) ────────────────────────────────────
    pl_dist = abs(pl - 0.5)
    min_dist_pl = _lgb_dist_min_up() if direction == "UP" else _lgb_dist_min_down()
    allow_lgb_bypass = (
        is_vhc and direction == "UP" and _vhc_bypass_up_dist()
    )
    if pl_dist < min_dist_pl:
        if allow_lgb_bypass:
            gates.append(
                _gate(
                    "lgb_safety_floor",
                    True,
                    f"UP bypass by VHC: pl_dist={pl_dist:.3f} < "
                    f"{min_dist_pl:.3f} but pc_dist={pc_dist:.3f} "
                    f">= {_vhc_threshold():.2f}",
                )
            )
            vhc_bypasses.append("up_dist_floor")
        else:
            gates.append(
                _gate(
                    "lgb_safety_floor",
                    False,
                    f"{direction} pl_dist={pl_dist:.3f} < "
                    f"min {min_dist_pl:.3f}",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"lgb_safety_floor: {direction} pl_dist={pl_dist:.3f} "
                f"< {min_dist_pl:.3f}",
                gates,
                direction=direction,
            )
    else:
        gates.append(
            _gate(
                "lgb_safety_floor",
                True,
                f"{direction} pl_dist={pl_dist:.3f} >= {min_dist_pl:.3f}",
            )
        )

    # ── 11. TRANSITION regime block (R5) — VHC OR strong-oracle bypassable ─
    # Two independent bypass paths:
    #   (a) VHC reinforcement (pc_dist >= vhc_threshold + pc/pl direction agree)
    #   (b) Strong-oracle bypass (chainlink+tiingo agree + |Δ| strong + LGB
    #       dist strong) — note #228, recovers high-conviction TRANSITION
    #       windows the symmetric block would otherwise drop.
    vpin_regime = getattr(surface, "regime", None)
    blocked_down = vpin_regime in _block_down_vpin_regimes()
    blocked_up = vpin_regime in _block_up_vpin_regimes()
    regime_blocked = (direction == "DOWN" and blocked_down) or (
        direction == "UP" and blocked_up
    )
    if regime_blocked:
        vhc_bypass_ok = is_vhc and _vhc_bypass_transition()
        strong_bypass_ok, strong_diag = _should_bypass_transition(
            direction,
            pl,
            surface.delta_chainlink,
            surface.delta_tiingo,
        )
        if vhc_bypass_ok:
            gates.append(
                _gate(
                    "vpin_regime_direction",
                    True,
                    f"{direction} bypassed {vpin_regime} by VHC "
                    f"(pc_dist={pc_dist:.3f} >= {_vhc_threshold():.2f})",
                )
            )
            vhc_bypasses.append("transition_regime")
        elif strong_bypass_ok:
            gates.append(
                _gate(
                    "vpin_regime_direction",
                    True,
                    f"transition_strong_bypass: {direction} in "
                    f"{vpin_regime} {strong_diag}",
                )
            )
        else:
            gates.append(
                _gate(
                    "vpin_regime_direction",
                    False,
                    f"{direction} blocked in vpin regime={vpin_regime} "
                    f"(no bypass: {strong_diag})",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"vpin_regime_direction: {direction} blocked in {vpin_regime}",
                gates,
                direction=direction,
            )
    else:
        gates.append(
            _gate(
                "vpin_regime_direction",
                True,
                f"{direction} allowed in vpin regime={vpin_regime}",
            )
        )

    # ── 12. Oracle direction agreement ─────────────────────────────────────
    if _skip_on_oracle_disagree():
        cl_delta = surface.delta_chainlink or 0.0
        ti_delta = surface.delta_tiingo or 0.0
        cl_dir = "UP" if cl_delta > 0 else "DOWN"
        ti_dir = "UP" if ti_delta > 0 else "DOWN"
        if direction != cl_dir or direction != ti_dir:
            gates.append(
                _gate(
                    "oracle_direction",
                    False,
                    f"cl={cl_dir} ti={ti_dir} vs {direction}",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"oracle_direction: disagree cl={cl_dir} ti={ti_dir} "
                f"vs {direction}",
                gates,
                direction=direction,
            )
    gates.append(
        _gate("oracle_direction", True, f"oracles agree with {direction}")
    )

    # ── 13. Fill band (R7) ─────────────────────────────────────────────────
    if direction == "UP":
        fill_price = getattr(surface, "clob_up_ask", None)
    else:
        fill_price = getattr(surface, "clob_down_ask", None)
    if fill_price is None:
        fill_price = getattr(surface, "poly_max_entry_price", None)
    if fill_price is None:
        gates.append(_gate("fill_band", False, "no CLOB ask / poly_max_entry"))
        reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
        return _skip_v9(
            "fill_band: no fill price available",
            gates,
            direction=direction,
        )

    fmin = _fill_band_min_lgb()
    fmax = _fill_band_max()
    if fill_price < fmin or fill_price > fmax:
        gates.append(
            _gate(
                "fill_band",
                False,
                f"fill={fill_price:.3f} outside [{fmin:.2f},{fmax:.2f}]",
            )
        )
        reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
        return _skip_v9(
            f"fill_band: price={fill_price:.3f} outside "
            f"[{fmin:.2f},{fmax:.2f}]",
            gates,
            direction=direction,
        )
    gates.append(
        _gate(
            "fill_band",
            True,
            f"fill={fill_price:.3f} in [{fmin:.2f},{fmax:.2f}]",
        )
    )

    # ── 14a. UP fill floor (NOT VHC-bypassable) ────────────────────────────
    if direction == "UP":
        floor = _up_min_fill_price()
        if floor > 0 and fill_price < floor:
            gates.append(
                _gate(
                    "up_fill_floor",
                    False,
                    f"UP fill={fill_price:.3f} < {floor:.2f}",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"up_fill_floor: fill={fill_price:.3f} < {floor:.2f}",
                gates,
                direction=direction,
            )
        gates.append(
            _gate(
                "up_fill_floor",
                True,
                f"UP fill={fill_price:.3f} >= {floor:.2f}",
            )
        )

    # ── 14b. DOWN fill floor (NOT VHC-bypassable) ──────────────────────────
    if direction == "DOWN":
        floor = _down_min_fill_price()
        if floor > 0 and fill_price < floor:
            gates.append(
                _gate(
                    "down_fill_floor",
                    False,
                    f"DOWN fill={fill_price:.3f} < {floor:.2f}",
                )
            )
            reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
            return _skip_v9(
                f"down_fill_floor: fill={fill_price:.3f} < {floor:.2f}",
                gates,
                direction=direction,
            )
        gates.append(
            _gate(
                "down_fill_floor",
                True,
                f"DOWN fill={fill_price:.3f} >= {floor:.2f}",
            )
        )

    # ── 15. Post-loss cooldown (NOT VHC-bypassable) ────────────────────────
    in_cd, remaining = _in_cooldown(now)
    if in_cd:
        gates.append(
            _gate(
                "post_loss_cooldown",
                False,
                f"{remaining}s remaining",
            )
        )
        reset_confirmation_v9(_STRATEGY_ID, getattr(surface, "window_ts", 0))
        return _skip_v9(
            f"post_loss_cooldown: {remaining // 60}m {remaining % 60}s "
            f"remaining",
            gates,
            direction=direction,
        )
    gates.append(
        _gate("post_loss_cooldown", True, "no active cooldown")
    )

    # ── 16. 3-tick entry confirmation (2026-04-24, note #298) ────────────
    _wts = getattr(surface, "window_ts", 0) or 0
    required = _min_consecutive_pass_ticks()
    if required > 0:
        confirmed = check_confirmation_v9(_STRATEGY_ID, _wts, direction)
        count = get_confirmation_count_v9(_STRATEGY_ID, _wts)
        if not confirmed:
            gates.append(
                _gate(
                    "entry_confirmation",
                    False,
                    f"{count}/{required} consecutive pass ticks",
                )
            )
            return _skip_v9(
                f"entry_confirmation: {count}/{required} ticks",
                gates,
                direction=direction,
            )
        gates.append(
            _gate(
                "entry_confirmation",
                True,
                f"{count}/{required} consecutive pass ticks — confirmed",
            )
        )

    # ── TRADE ──────────────────────────────────────────────────────────────
    label, score, is_vhc_final = _classify_conviction(pu, pc, pc_dir, pl_dir)
    confidence_level = (
        "HIGH" if label in ("VERY_HIGH", "HIGH") else "MEDIUM"
    )

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence_level,
        confidence_score=score,
        entry_cap=_ENTRY_CAP,
        collateral_pct=None,  # custom clob_sizing schedule decides
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            f"v9_ensemble_{direction}_T{surface.eval_offset}_"
            f"{label.lower()}_f{fill_price:.2f}"
        ),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": abs(pu - 0.5),
            "v4_regime": v4_regime,
            "vpin": vpin,
            "vpin_regime": vpin_regime,
            "probability_lgb": pl,
            "probability_classifier": pc,
            "probability_used": pu,
            "pc_weight": pc_weight,
            "pl_weight": pl_weight,
            "pc_dist": pc_dist,
            "pl_dist": pl_dist,
            "pu_dist": abs(pu - 0.5),
            "disagreement": disagreement,
            "conviction_label": label,
            "is_vhc": is_vhc_final,
            "vhc_bypasses": vhc_bypasses,
            "vhc_kelly_multiplier": (
                _vhc_kelly_multiplier() if is_vhc_final else None
            ),
            "fill_price": fill_price,
            "chainlink_delta": surface.delta_chainlink,
            "tiingo_delta": surface.delta_tiingo,
            "primary_signal_source": "ensemble",
            "exit_monitor_enabled": _exit_monitor_enabled(),
            "exit_params": {
                "exit_shadow_mode": _exit_shadow_mode(),
                "exit_min_hold_seconds": _exit_min_hold_seconds(),
                "exit_no_exit_last_seconds": _exit_no_exit_last_seconds(),
                "exit_consecutive_flip_ticks": _exit_consecutive_flip_ticks(),
                "exit_lgb_flip_enabled": _exit_lgb_flip_enabled(),
                "exit_oracle_flip_enabled": _exit_oracle_flip_enabled(),
                "exit_max_retries": _exit_max_retries(),
                "exit_retry_timeout_seconds": _exit_retry_timeout_seconds(),
            },
        },
    )


# Re-export cooldown controls for tests / outcome reconciler.
__all__ = [
    "evaluate_v9_ensemble",
    "record_loss",
    "reset_cooldown",
    "check_confirmation_v9",
    "get_confirmation_count_v9",
    "reset_confirmation_v9",
    "reset_all_confirmations_v9",
]
