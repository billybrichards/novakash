"""Custom hooks for v5_ensemble strategy (audit #121 Path 1).

Fork of v4_fusion.py with two targeted additions:
  1. Signal-source selection (ensemble / lgb_only / path1_only) via
     V5_ENSEMBLE_SIGNAL_SOURCE env var.
  2. Two new ensemble-specific gates inserted after the confidence gate:
        - ensemble_fallback_sanity (skip when ensemble fell back to LGB-only)
        - ensemble_disagreement     (skip when |p_lgb - p_path1| > threshold)

All 13 v4_fusion gates preserved verbatim — this is a signal swap, not a
safety change. The timesfm-repo side provides probability_lgb /
probability_classifier / ensemble_config on the v4 surface
(timesfm-repo commits d1f1965..f62e9d8). Cross-repo handoff: hub note 159.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.alert_logic import score_signal_health
from domain.alert_values import HealthStatus
from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v5_ensemble"
_VERSION = "5.1.0"

# Inherited from v4_fusion — non-env, not tunable.
_CONVICTION_THRESHOLDS = {
    "HIGH": 0.12,
    "MEDIUM": 0.15,
    "LOW": 0.20,
    "NONE": 1.0,
}
_TRADEABLE_REGIMES = {"calm_trend", "volatile_trend"}


# ── Tunable gate knobs (YAML gate_params → env fallback → default) ──────────
# v5 shares the v4 gate stack, so the first block mirrors v4_fusion's helper
# names (same env var names + defaults). The second block adds the
# v5-specific ensemble knobs. Per-strategy YAML overrides let v5_ensemble
# stay strict while a future v5_fresh runs relaxed on the same engine —
# that's the whole motivation for the gate_params refactor.
def _min_offset_sec() -> int:
    return _gp.get_int("min_offset_sec", "V4_MIN_OFFSET_SEC", 45)


def _risk_off_override_enabled() -> bool:
    return _gp.get_bool(
        "risk_off_override_enabled", "V4_RISK_OFF_OVERRIDE_ENABLED", True
    )


def _risk_off_override_dist_min() -> float:
    return _gp.get_float(
        "risk_off_override_dist_min", "V4_RISK_OFF_OVERRIDE_DIST_MIN", 0.20
    )


def _fusion_skip_calm() -> bool:
    return _gp.get_bool("skip_calm", "V4_FUSION_SKIP_CALM", True)


def _fusion_require_tiingo_agree() -> bool:
    return _gp.get_bool(
        "require_tiingo_agree", "V4_FUSION_REQUIRE_TIINGO_AGREE", True
    )


def _fusion_skip_stale_sources() -> bool:
    return _gp.get_bool(
        "skip_stale_sources", "V4_FUSION_SKIP_STALE_SOURCES", True
    )


def _fusion_health_gate() -> str:
    return _gp.get_str("health_gate", "V4_FUSION_HEALTH_GATE", "degraded").lower()


def _risk_off_override_require_tiingo() -> bool:
    return _gp.get_bool(
        "risk_off_override_require_tiingo",
        "V4_RISK_OFF_OVERRIDE_REQUIRE_TIINGO",
        True,
    )


# v5-specific ensemble knobs
def _ensemble_source() -> str:
    # ensemble | lgb_only | path1_only
    return _gp.get_str(
        "ensemble_signal_source", "V5_ENSEMBLE_SIGNAL_SOURCE", "ensemble"
    ).lower()


def _ensemble_disagreement_threshold() -> float:
    # 0 = gate disabled. Enable at ~0.15-0.20 once baseline shadow is read.
    return _gp.get_float(
        "ensemble_disagreement_threshold",
        "V5_ENSEMBLE_DISAGREEMENT_THRESHOLD",
        0.0,
    )


def _skip_on_ensemble_fallback() -> bool:
    return _gp.get_bool(
        "ensemble_skip_on_fallback", "V5_ENSEMBLE_SKIP_ON_FALLBACK", True
    )


def _gate(name: str, passed: bool, reason: str) -> dict:
    return {"gate": name, "passed": passed, "reason": reason}


def _sources_agree_surface(surface: "FullDataSurface") -> Optional[bool]:
    """Mirror of v4_fusion._sources_agree_surface — chainlink + tiingo agreement."""
    cl = surface.delta_chainlink
    ti = surface.delta_tiingo
    if cl is None or ti is None:
        return None
    cl_sign = 1 if cl > 0 else (-1 if cl < 0 else 0)
    ti_sign = 1 if ti > 0 else (-1 if ti < 0 else 0)
    return cl_sign == ti_sign


def _compute_health_badge(
    surface: "FullDataSurface",
    distance: float,
    direction: Optional[str],
    risk_off_override_active: bool,
):
    """Mirror of v4_fusion._compute_health_badge."""
    confidence_label = surface.v4_conviction if surface.v4_conviction else None
    eval_band_in_optimal = (surface.poly_timing == "optimal")
    return score_signal_health(
        vpin=surface.vpin,
        p_up=(0.5 + distance if direction == "UP"
              else (0.5 - distance if direction == "DOWN" else None)),
        p_up_distance=distance,
        sources_agree=_sources_agree_surface(surface),
        confidence_label=confidence_label,
        confidence_override_active=risk_off_override_active,
        eval_band_in_optimal=eval_band_in_optimal,
        chainlink_feed_age_s=None,
    )


def _try_risk_off_override(
    reason: str,
    distance: float,
    direction: Optional[str],
    surface: "FullDataSurface",
    gates: list[dict],
) -> bool:
    """Mirror of v4_fusion._try_risk_off_override (verbatim logic)."""
    if not _risk_off_override_enabled():
        return False
    if "risk_off" not in (reason or ""):
        return False
    dist_min = _risk_off_override_dist_min()
    if distance < dist_min:
        gates.append(
            _gate(
                "regime_risk_off_override",
                False,
                f"dist={distance:.3f} < {dist_min:.2f} conviction floor",
            )
        )
        return False
    if direction is None:
        return False
    cl_delta = surface.delta_chainlink
    if cl_delta is None:
        gates.append(
            _gate(
                "regime_risk_off_override",
                False,
                "no chainlink delta to verify direction alignment",
            )
        )
        return False
    cl_direction = "UP" if cl_delta > 0 else "DOWN"
    if cl_direction != direction:
        gates.append(
            _gate(
                "regime_risk_off_override",
                False,
                f"chainlink={cl_direction} disagrees with trade={direction}",
            )
        )
        return False

    if _risk_off_override_require_tiingo():
        ti_delta = surface.delta_tiingo
        if ti_delta is None:
            gates.append(
                _gate(
                    "regime_risk_off_override",
                    False,
                    "tiingo unavailable — override requires both sources",
                )
            )
            return False
        ti_direction = "UP" if ti_delta > 0 else "DOWN"
        if ti_direction != direction:
            gates.append(
                _gate(
                    "regime_risk_off_override",
                    False,
                    f"tiingo={ti_direction} disagrees with trade={direction}",
                )
            )
            return False

    gates.append(
        _gate(
            "regime_risk_off_override",
            True,
            (
                f"risk_off overridden: dist={distance:.3f} "
                f">= {_risk_off_override_dist_min():.2f}, "
                f"chainlink={cl_direction} + tiingo aligns with trade={direction}"
            ),
        )
    )
    return True


def _skip(reason: str, gate_results: Optional[list[dict]] = None) -> StrategyDecision:
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
        metadata={"gate_results": gate_results or []},
    )


def _extract_ensemble_fields(surface: "FullDataSurface"):
    """Pull the cross-repo ensemble fields from the FullDataSurface.

    Uses getattr so a pre-upgrade DataSurface (without these attrs) degrades
    gracefully — important for unit tests using lightweight stubs.
    """
    return (
        getattr(surface, "probability_lgb", None),
        getattr(surface, "probability_classifier", None),
        getattr(surface, "ensemble_config", None),
    )


def evaluate_polymarket_ensemble(
    surface: "FullDataSurface",
) -> Optional[StrategyDecision]:
    """Entry point mirroring v4_fusion.evaluate_polymarket_v2."""
    if surface.poly_direction is not None:
        return _evaluate_poly_v2_ensemble(surface)
    if surface.v4_recommended_side in ("UP", "DOWN"):
        return _evaluate_poly_legacy(surface)
    return _evaluate_legacy(surface)


def _evaluate_poly_v2_ensemble(surface: "FullDataSurface") -> StrategyDecision:
    """v5_ensemble evaluation — fork of v4_fusion._evaluate_poly_v2.

    Differences vs v4_fusion:
      1. Confidence / distance derived from configurable source
         (ensemble / lgb_only / path1_only).
      2. Two new gates inserted AFTER the existing confidence gate:
         - ensemble_fallback_sanity
         - ensemble_disagreement
      All other gates inherited verbatim.
    """
    direction = surface.poly_direction
    trade_advised = surface.poly_trade_advised or False
    timing = surface.poly_timing or "unknown"
    reason = surface.poly_reason or "unknown"
    max_entry = surface.poly_max_entry_price
    gates: list[dict] = []

    # ── Freshness gate ─────────────────────────────────────────────────────
    # Skip when surface is too old. Protects against engine stalls /
    # feed brown-outs masking as live data. Default 60s = matches v4 cache
    # rejection in data_surface.py. Tighten via YAML for stricter live paths.
    import time as _time
    max_age = _gp.get_float(
        "max_surface_age_seconds", "V5_MAX_SURFACE_AGE_SEC", 60.0
    )
    assembled_at = getattr(surface, "assembled_at", None)
    if assembled_at and max_age > 0:
        age = _time.time() - float(assembled_at)
        if age > max_age:
            gates.append(
                _gate("surface_freshness", False, f"age={age:.1f}s > {max_age:.0f}s")
            )
            return _skip(f"surface_stale: age={age:.1f}s > {max_age:.0f}s", gates)
        gates.append(_gate("surface_freshness", True, f"age={age:.1f}s"))

    # ── UTC hour block (gate_params: block_utc_hours: [7,8,9]). ────────────
    # Empty list = no block. Previously only wired in _evaluate_legacy;
    # ported here so it actually applies to v5_ensemble / v5_fresh.
    block_hours = _gp.get_int_list("block_utc_hours", "V4_BLOCK_UTC_HOURS", [])
    if block_hours:
        window_ts = getattr(surface, "window_ts", None)
        if window_ts:
            import datetime as _dt
            hour = _dt.datetime.fromtimestamp(int(window_ts), _dt.timezone.utc).hour
            if hour in block_hours:
                gates.append(
                    _gate("utc_hour_block", False, f"hour={hour} in {sorted(block_hours)}")
                )
                return _skip(f"utc_hour_block hour={hour}", gates)

    # ── v4_regime allowlist (gate_params: tradeable_v4_regimes). ───────────
    # Default = {calm_trend, volatile_trend} matching _TRADEABLE_REGIMES.
    # 7d audit (2026-04-19): chop = 45.5% WR (noise) → exclude chop in YAML.
    # Previously only wired in _evaluate_legacy; ported here for primary path.
    v4_regime = surface.v4_regime
    tradeable = set(
        _gp.get_str_list(
            "tradeable_v4_regimes",
            "V4_TRADEABLE_REGIMES",
            list(_TRADEABLE_REGIMES),
        )
    )
    if v4_regime and v4_regime not in tradeable:
        gates.append(
            _gate("v4_regime", False, f"regime={v4_regime} not in {sorted(tradeable)}")
        )
        return _skip(f"v4_regime={v4_regime} not tradeable", gates)

    # ── NEW: pull ensemble fields ──────────────────────────────────────────
    p_lgb, p_classifier, ens_cfg = _extract_ensemble_fields(surface)

    # ── NEW: select signal source per V5_ENSEMBLE_SIGNAL_SOURCE ────────────
    source = _ensemble_source()
    if source == "lgb_only":
        # If timesfm has ensemble disabled, p_lgb is None → poly_confidence
        # IS the LGB calibrated value, so fall back to it.
        confidence = p_lgb if p_lgb is not None else (surface.poly_confidence or 0.5)
    elif source == "path1_only":
        if p_classifier is None:
            gates.append(
                _gate(
                    "ensemble_signal_source",
                    False,
                    "path1_only requested but classifier p_up unavailable",
                )
            )
            return _skip("path1_only: classifier unavailable", gates)
        confidence = p_classifier
    else:  # "ensemble" (default) — read the blended value from poly_confidence
        confidence = surface.poly_confidence or 0.5

    distance = abs(confidence - 0.5)
    gates.append(
        _gate(
            "ensemble_signal_source",
            True,
            f"source={source} conf={confidence:.3f} dist={distance:.3f}",
        )
    )

    # ── Inherited v4_fusion gates start here (unchanged) ───────────────────

    # CALM regime skip (audit #225)
    skip_calm = _fusion_skip_calm()
    vpin_regime = surface.regime
    if skip_calm and vpin_regime == "CALM":
        gates.append(
            _gate(
                "calm_regime",
                False,
                "regime=CALM v2 model 45.6% accuracy, skip",
            )
        )
        return _skip("calm_regime_model_underperforms", gates)
    if not skip_calm and vpin_regime == "CALM":
        gates.append(
            _gate("calm_regime", True, "calm_gate_disabled_by_env (regime=CALM)")
        )
    elif vpin_regime is not None:
        gates.append(_gate("calm_regime", True, f"regime={vpin_regime} tradeable"))

    # Feature staleness (audit #234)
    if _fusion_skip_stale_sources():
        missing_sources = []
        if surface.delta_chainlink is None:
            missing_sources.append("chainlink")
        if surface.delta_tiingo is None:
            missing_sources.append("tiingo")
        if missing_sources:
            gates.append(
                _gate(
                    "feature_staleness",
                    False,
                    f"sources_missing: {','.join(missing_sources)}",
                )
            )
            return _skip(
                f"feature_stale: {','.join(missing_sources)} missing at eval",
                gates,
            )
        gates.append(_gate("feature_staleness", True, "chainlink + tiingo present"))

    # Execution timing
    min_offset = _min_offset_sec()
    offset = surface.eval_offset or 0
    if offset < min_offset:
        gates.append(
            _gate(
                "execution_timing",
                False,
                f"T-{offset} < T-{min_offset} live minimum",
            )
        )
        return _skip(
            f"polymarket: timing={timing} T-{offset} -- too late "
            f"(<{min_offset}s), skip",
            gates,
        )

    # Timing buckets: early / expired / late_window / late
    if timing in ("expired", "early"):
        gates.append(_gate("timing", False, f"timing={timing} outside trade window"))
        return _skip(f"polymarket: timing={timing} -- outside window", gates)
    if timing == "late_window":
        clob_implied = surface.clob_implied_up
        if clob_implied is not None:
            divergence = distance - abs(float(clob_implied) - 0.5)
            if divergence < 0.04:
                gates.append(
                    _gate(
                        "late_window_divergence",
                        False,
                        f"div={divergence:.3f} < 0.04",
                    )
                )
                return _skip(
                    f"polymarket: late_window but CLOB already priced "
                    f"(div={divergence:.3f} < 0.04)",
                    gates,
                )
        else:
            gates.append(_gate("late_window_divergence", False, "no CLOB data"))
            return _skip("polymarket: late_window but no CLOB data", gates)
    if timing == "late":
        gates.append(_gate("timing", False, "timing=late outside window"))
        return _skip(f"polymarket: timing=late -- outside window", gates)

    # Confidence gate — tunable per strategy via gate_params. Default 0.12
    # = zero behaviour change. Hub note #183 showed mid-conviction
    # (dist 0.0-0.20) was the bleeding bucket; raise via YAML per strategy.
    conf_min = _gp.get_float("confidence_min_distance", "V5_CONFIDENCE_MIN_DIST", 0.12)
    if distance < conf_min:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < {conf_min:.2f}"))
        return _skip(
            f"polymarket: p_up={confidence:.3f} dist={distance:.3f} < {conf_min:.2f} threshold",
            gates,
        )
    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= {conf_min:.2f}"))

    # ── NEW ensemble-specific gates ────────────────────────────────────────
    # Fallback sanity — skip when ensemble degraded to LGB-only by default.
    skip_fallback = _skip_on_ensemble_fallback()
    if (
        skip_fallback
        and ens_cfg
        and ens_cfg.get("mode") == "fallback_lgb_only"
    ):
        gates.append(
            _gate(
                "ensemble_fallback_sanity",
                False,
                "ensemble degraded to fallback_lgb_only; classifier unavailable",
            )
        )
        return _skip("ensemble_fallback_lgb_only", gates)
    if ens_cfg and ens_cfg.get("mode") == "fallback_lgb_only":
        gates.append(
            _gate(
                "ensemble_fallback_sanity",
                True,
                "fallback accepted (ensemble_skip_on_fallback=false)",
            )
        )

    # Disagreement abstain — disabled by default (threshold=0).
    disagreement_threshold = _ensemble_disagreement_threshold()
    if (
        disagreement_threshold > 0
        and p_lgb is not None
        and p_classifier is not None
    ):
        p_lgb_f = float(p_lgb)
        p_cls_f = float(p_classifier)
        disagreement = abs(p_lgb_f - p_cls_f)
        # Surface which model predicted which direction so the operator
        # can tell at a glance whether the old (lgb) or new (path1
        # classifier) was the dissenting source.
        lgb_dir = "UP" if p_lgb_f > 0.5 else "DOWN"
        p1_dir = "UP" if p_cls_f > 0.5 else "DOWN"
        detail = (
            f"lgb={p_lgb_f:.2f}({lgb_dir}) path1={p_cls_f:.2f}({p1_dir}) "
            f"|Δ|={disagreement:.3f}"
        )
        if disagreement > disagreement_threshold:
            gates.append(
                _gate(
                    "ensemble_disagreement",
                    False,
                    f"{detail} > thr={disagreement_threshold:.3f}",
                )
            )
            return _skip(f"ensemble_disagreement: {detail}", gates)
        gates.append(
            _gate(
                "ensemble_disagreement",
                True,
                f"models agree ({detail})",
            )
        )

    # ── Inherited v4_fusion gates continue (unchanged) ─────────────────────
    risk_off_overridden = False
    if not trade_advised:
        if _try_risk_off_override(reason, distance, direction, surface, gates):
            risk_off_overridden = True
        else:
            gates.append(_gate("trade_advised", False, f"{reason} (timing={timing})"))
            return _skip(
                f"polymarket: {reason} (timing={timing}, dist={distance:.3f})",
                gates,
            )
    if risk_off_overridden:
        gates.append(
            _gate(
                "trade_advised",
                True,
                "trade_advised=false but risk_off_override applied",
            )
        )
    else:
        gates.append(_gate("trade_advised", True, "trade_advised=true"))

    if not direction:
        gates.append(_gate("direction", False, "no direction"))
        return _skip("polymarket: no direction", gates)
    gates.append(_gate("direction", True, f"direction={direction}"))

    # Macro direction gate
    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)
    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    # Chainlink agreement
    if surface.delta_chainlink is not None:
        cl_direction = "UP" if surface.delta_chainlink > 0 else "DOWN"
        if cl_direction != direction:
            gates.append(
                _gate(
                    "chainlink_agreement",
                    False,
                    f"oracle={cl_direction} vs trade={direction}",
                )
            )
            return _skip(
                f"chainlink_disagrees: oracle={cl_direction} vs trade={direction} "
                f"(cl_delta={surface.delta_chainlink:+.5f})",
                gates,
            )
        gates.append(_gate("chainlink_agreement", True, "Chainlink agrees"))

    # Tiingo agreement (audit #228)
    if not _fusion_require_tiingo_agree():
        gates.append(_gate("tiingo_agreement", True, "tiingo_gate_disabled_by_env"))
    elif surface.delta_tiingo is None:
        gates.append(_gate("tiingo_agreement", True, "tiingo_unavailable"))
    else:
        tiingo_direction = "UP" if surface.delta_tiingo > 0 else "DOWN"
        if tiingo_direction != direction:
            gates.append(
                _gate(
                    "tiingo_agreement",
                    False,
                    f"tiingo={tiingo_direction} vs trade={direction}",
                )
            )
            return _skip(
                f"tiingo_disagrees: tiingo={tiingo_direction} vs trade={direction} "
                f"(tiingo_delta={surface.delta_tiingo:+.5f})",
                gates,
            )
        gates.append(_gate("tiingo_agreement", True, "Tiingo agrees"))

    # HealthBadge gate (audit #223)
    health_gate = _fusion_health_gate()
    if health_gate != "off":
        health = _compute_health_badge(
            surface, distance, direction, risk_off_overridden,
        )
        block_on = {
            "unsafe": {HealthStatus.UNSAFE},
            "degraded": {HealthStatus.DEGRADED, HealthStatus.UNSAFE},
        }.get(health_gate, set())
        if health.status in block_on:
            gates.append(
                _gate(
                    "health_badge",
                    False,
                    f"status={health.status.value} reasons={','.join(health.reasons)}",
                )
            )
            return _skip(
                f"health_{health.status.value.lower()}: "
                f"{','.join(health.reasons) if health.reasons else 'unspecified'}",
                gates,
            )
        gates.append(
            _gate(
                "health_badge",
                True,
                f"status={health.status.value} "
                f"reasons={','.join(health.reasons) if health.reasons else 'clean'}",
            )
        )

    entry_reason_prefix = (
        "polymarket_ensemble_override_" if risk_off_overridden
        else "polymarket_ensemble_"
    )
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=surface.v4_conviction or f"dist_{distance:.2f}",
        confidence_score=distance * 2.0,
        entry_cap=max_entry,
        collateral_pct=surface.v4_recommended_collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"{entry_reason_prefix}{reason}_T{surface.eval_offset}",
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": distance,
            "poly_timing": timing,
            "v4_regime": surface.v4_regime,
            "vpin_regime": surface.regime,
            "chainlink_delta": surface.delta_chainlink,
            "chainlink_agrees": True,
            "tiingo_delta": surface.delta_tiingo,
            "tiingo_agrees": True,
            "risk_off_overridden": risk_off_overridden,
            # v5_ensemble-specific audit trail
            "signal_source": source,
            "probability_lgb": p_lgb,
            "probability_classifier": p_classifier,
            "probability_used": confidence,
            "ensemble_config": ens_cfg,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy paths — copied verbatim from v4_fusion.py:575-700. Pure inheritance.
# ─────────────────────────────────────────────────────────────────────────────


def _evaluate_poly_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy polymarket path for old timesfm builds (verbatim from v4_fusion)."""
    p_up = surface.v2_probability_up or 0.5
    distance = abs(p_up - 0.5)
    direction = surface.v4_recommended_side or ("UP" if p_up > 0.5 else "DOWN")
    gates: list[dict] = []

    conf_min = _gp.get_float("confidence_min_distance", "V5_CONFIDENCE_MIN_DIST", 0.12)
    if distance < conf_min:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < {conf_min:.2f}"))
        return _skip(f"polymarket_legacy: dist={distance:.3f} < {conf_min:.2f}", gates)

    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= {conf_min:.2f}"))

    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)

    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    collateral_pct = surface.v4_recommended_collateral_pct
    if collateral_pct is not None and surface.v4_macro_size_modifier:
        collateral_pct *= surface.v4_macro_size_modifier

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=surface.v4_conviction,
        confidence_score=surface.v4_conviction_score or (distance * 2.0),
        entry_cap=surface.poly_max_entry_price,
        collateral_pct=collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"polymarket_legacy_dist{distance:.2f}_T{surface.eval_offset}",
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "v2_probability_up": p_up,
            "v4_regime": surface.v4_regime,
        },
    )


def _evaluate_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy margin-engine path (verbatim from v4_fusion)."""
    regime = surface.v4_regime
    gates: list[dict] = []

    # UTC hour block (per-strategy gate_params, e.g. block_utc_hours: [7,8,9]).
    # Empty list = no block. Used by v5_ensemble to avoid 07-09 UTC (0-25% WR).
    block_hours = _gp.get_int_list("block_utc_hours", "V4_BLOCK_UTC_HOURS", [])
    if block_hours:
        window_ts = getattr(surface, "window_ts", None)
        if window_ts:
            import datetime as _dt
            hour = _dt.datetime.fromtimestamp(int(window_ts), _dt.timezone.utc).hour
            if hour in block_hours:
                gates.append(_gate("utc_hour_block", False, f"hour={hour} in blocked {sorted(block_hours)}"))
                return _skip(f"utc_hour_block hour={hour}", gates)

    # Tradeable v4_regime set (per-strategy gate_params).
    # Default = {calm_trend, volatile_trend}. v5_fresh drops calm_trend (40% WR).
    tradeable = set(
        _gp.get_str_list(
            "tradeable_v4_regimes",
            "V4_TRADEABLE_REGIMES",
            list(_TRADEABLE_REGIMES),
        )
    )
    if regime and regime not in tradeable:
        gates.append(
            _gate("regime", False, f"regime={regime} not in tradeable={sorted(tradeable)}")
        )
        return _skip(f"regime={regime} not tradeable", gates)

    gates.append(_gate("regime", True, f"regime={regime} tradeable"))

    if not surface.v4_consensus_safe_to_trade:
        gates.append(_gate("consensus", False, "consensus not safe_to_trade"))
        return _skip("consensus not safe_to_trade", gates)

    gates.append(_gate("consensus", True, "safe_to_trade=true"))

    p_up = surface.v2_probability_up
    if p_up is None:
        gates.append(_gate("confidence", False, "no probability_up"))
        return _skip("no probability_up", gates)
    distance = abs(p_up - 0.5)
    conviction = surface.v4_conviction or "NONE"
    min_dist = _CONVICTION_THRESHOLDS.get(conviction, 1.0)
    if distance < min_dist:
        gates.append(
            _gate("confidence", False, f"dist={distance:.2f} < {min_dist:.2f}")
        )
        return _skip(
            f"conviction={conviction} requires dist={min_dist:.2f}, got {distance:.2f}",
            gates,
        )

    gates.append(_gate("confidence", True, f"dist={distance:.2f} >= {min_dist:.2f}"))

    direction = surface.v4_recommended_side
    if direction is None:
        direction = "UP" if p_up > 0.5 else "DOWN"

    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)

    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    collateral_pct = surface.v4_recommended_collateral_pct
    if collateral_pct is not None and surface.v4_macro_size_modifier:
        collateral_pct *= surface.v4_macro_size_modifier

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=conviction,
        confidence_score=surface.v4_conviction_score,
        entry_cap=None,
        collateral_pct=collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(f"v4_{conviction}_{regime}_T{surface.eval_offset}_p{p_up:.2f}"),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "v2_probability_up": p_up,
            "v4_regime": regime,
            "v4_conviction": conviction,
        },
    )
