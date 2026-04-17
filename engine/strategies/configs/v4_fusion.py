"""Custom hooks for v4_fusion strategy.

Ports the polymarket_v2 evaluation path from V4FusionStrategy.
This handles the complex timing/CLOB-divergence logic that doesn't
reduce to simple gate configs.

Three evaluation paths:
1. polymarket_v2: clean venue-specific recommendation (preferred)
2. polymarket legacy: old timesfm builds with venue="polymarket" in extras
3. legacy margin-engine: regime + consensus + conviction gates
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.alert_logic import score_signal_health
from domain.alert_values import HealthStatus
from domain.decision_metadata import DecisionMetadata
from domain.value_objects import StrategyDecision

_STRATEGY_ID = "v4_fusion"
_VERSION = "4.5.0"

# Conviction -> minimum distance from 0.5 (legacy path only)
_CONVICTION_THRESHOLDS = {
    "HIGH": 0.12,
    "MEDIUM": 0.15,
    "LOW": 0.20,
    "NONE": 1.0,
}

_TRADEABLE_REGIMES = {"calm_trend", "volatile_trend"}

# ── v4.3.0 Execution timing cutoff ───────────────────────────────────────────
# Minimum eval_offset (seconds before window close) to allow a trade. The
# sister repo's v2 model has peak accuracy at T-30 (86%) and T-60 (80%). Real
# fill latency is ~600ms FOK + ~20ms network (same ca-central-1 region), so
# we can safely trade until T-45 without risk of missing the close. The older
# T-70 floor left ~8s of the highest-conviction band unused.
_MIN_OFFSET_SEC = int(os.environ.get("V4_MIN_OFFSET_SEC", "45"))

# ── v4.3.0 Direction-aware risk_off override ─────────────────────────────────
# The sister repo (novakash-timesfm-repo/app/v4_strategies.py:952) short-
# circuits ALL trades when HMM regime == risk_off, even HIGH-conviction ones.
# During directional regime spikes (e.g. overnight rally), this leaves
# aligned-direction wins on the table. The override allows the engine to
# take the trade IFF: (a) sister's skip reason contains "risk_off",
# (b) distance >= _RISK_OFF_OVERRIDE_DIST_MIN (HIGH conviction),
# (c) Chainlink 5m delta direction agrees with the trade direction.
# Disable by setting V4_RISK_OFF_OVERRIDE_ENABLED=false.
_RISK_OFF_OVERRIDE_ENABLED = (
    os.environ.get("V4_RISK_OFF_OVERRIDE_ENABLED", "true").lower() == "true"
)
_RISK_OFF_OVERRIDE_DIST_MIN = float(
    os.environ.get("V4_RISK_OFF_OVERRIDE_DIST_MIN", "0.20")
)

# ── v4.4.0 CALM regime skip gate (audit task #225) ───────────────────────────
# Per project_v9_analysis.md, v2 model accuracy in regime=CALM is 45.6% (below
# coin flip). Other regimes are healthy: CASCADE 70.3%, TRANSITION 72.5%,
# NORMAL 73.3%. Default ON (safer). Disable via V4_FUSION_SKIP_CALM=false.
_FUSION_SKIP_CALM = (
    os.environ.get("V4_FUSION_SKIP_CALM", "true").lower() == "true"
)

# ── v4.4.0 Tiingo agreement gate (audit task #228) ───────────────────────────
# Mirrors the existing chainlink_agreement gate. If Tiingo's 5m delta points
# opposite to the trade direction, skip — the chainlink gate was historically
# the only directional source-agreement check. Today's 10:25 UTC near-miss:
# model DOWN HIGH conviction vs chainlink+tiingo UP — only magnitude gate
# saved us. Default ON (safer). Disable via V4_FUSION_REQUIRE_TIINGO_AGREE=false.
_FUSION_REQUIRE_TIINGO_AGREE = (
    os.environ.get("V4_FUSION_REQUIRE_TIINGO_AGREE", "true").lower() == "true"
)

# ── v4.5.0 Feature staleness skip gate (audit #234 + new) ────────────────────
# 7d DB-verified 2026-04-17: 21.67% of BTC 5m windows had chainlink OR tiingo
# NULL at evaluation time. Accuracy on stale rows = 54.8% vs clean 60.1% =
# 5.3pp gap. Using NULL-proxy semantics (no freshness_ms fields on
# FullDataSurface yet). A source being None is taken as "missing / stale /
# unusable". Default ON. Env toggle: V4_FUSION_SKIP_STALE_SOURCES.
_FUSION_SKIP_STALE_SOURCES = (
    os.environ.get("V4_FUSION_SKIP_STALE_SOURCES", "true").lower() == "true"
)

# ── v4.5.0 HealthBadge skip gate (audit task #223) ───────────────────────────
# engine/domain/alert_logic.py::score_signal_health() returns a HealthBadge
# with status OK / DEGRADED / UNSAFE + reasons tuple. Until this gate, the
# badge was rendered in TG but not read by the skip path — trades fired on
# DEGRADED inputs (see 11:57 UTC 2026-04-17 trade 0x377cbaaa: health
# DEGRADED [sources:unknown], FAK filled, LOSS -$2.46).
#
# Values:
#   "off"      — never skip on health (legacy behaviour)
#   "unsafe"   — skip only when status == UNSAFE (2+ amber OR red flag)
#   "degraded" — skip on DEGRADED or UNSAFE (any amber flag; strict default)
# Default "degraded" because the 11:57 loss had DEGRADED not UNSAFE and the
# current thin-source class of losses needs blocking.
_FUSION_HEALTH_GATE = os.environ.get("V4_FUSION_HEALTH_GATE", "degraded").lower()

# ── v4.5.0 Risk-off override hardening (audit #228 extension) ────────────────
# The v4.3.0 override path required ONLY chainlink to agree with trade
# direction. Today's 11:57 UTC override trade (0x377cbaaa) lost with health
# DEGRADED [sources:unknown] — Tiingo returned None so only chainlink was
# verifying. Override fired anyway. Under one-source confirmation an
# override is weak. v4.5.0 also requires Tiingo to be present and
# direction-aligned for the override to fire.
# Disable the extra check via V4_RISK_OFF_OVERRIDE_REQUIRE_TIINGO=false.
_RISK_OFF_OVERRIDE_REQUIRE_TIINGO = (
    os.environ.get("V4_RISK_OFF_OVERRIDE_REQUIRE_TIINGO", "true").lower() == "true"
)


def _gate(name: str, passed: bool, reason: str) -> dict:
    return {"gate": name, "passed": passed, "reason": reason}


def _sources_agree_surface(surface: "FullDataSurface") -> Optional[bool]:
    """Return True/False if chainlink + tiingo agree, None if either missing.

    Used for HealthBadge computation. Only uses chainlink + tiingo (the
    direction-agreement sources the strategy-level gates actually care about).
    Binance is primary price feed, not used for direction consensus at this
    layer.
    """
    cl = surface.delta_chainlink
    ti = surface.delta_tiingo
    if cl is None or ti is None:
        return None
    cl_sign = 1 if cl > 0 else (-1 if cl < 0 else 0)
    ti_sign = 1 if ti > 0 else (-1 if ti < 0 else 0)
    return cl_sign == ti_sign


def _compute_health_badge(surface: "FullDataSurface", distance: float,
                          direction: Optional[str],
                          risk_off_override_active: bool):
    """Compute HealthBadge from surface inputs for gating.

    Inlined rather than threaded through because the DataSurfaceManager
    does not currently build HealthBadge objects. Inputs match
    score_signal_health's signature.
    """
    # chainlink feed age isn't on the surface yet (would require freshness
    # fields — audit #234 phase 2). Pass None so feed-staleness dim falls
    # through rather than falsely green.
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
    """Return True if the risk_off veto from the sister repo should be overridden.

    Override preconditions (all required):
      1. Feature flag V4_RISK_OFF_OVERRIDE_ENABLED is true (default true).
      2. Sister's skip reason contains the substring 'risk_off'.
      3. Distance from 0.5 >= _RISK_OFF_OVERRIDE_DIST_MIN (HIGH conviction).
      4. Direction is set.
      5. Chainlink 5m delta direction matches the trade direction.

    All 5 checks append gate-trace rows so the decision path is auditable.
    """
    if not _RISK_OFF_OVERRIDE_ENABLED:
        return False
    if "risk_off" not in (reason or ""):
        return False
    if distance < _RISK_OFF_OVERRIDE_DIST_MIN:
        gates.append(
            _gate(
                "regime_risk_off_override",
                False,
                f"dist={distance:.3f} < {_RISK_OFF_OVERRIDE_DIST_MIN:.2f} conviction floor",
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

    # v4.5.0: override hardening — also require Tiingo agreement. Under
    # one-source confirmation an override is weak: today's 11:57 UTC LOSS
    # (order 0x377cbaaa) was an override-path trade with Tiingo None and
    # only chainlink verifying. Require both if the env flag is on.
    if _RISK_OFF_OVERRIDE_REQUIRE_TIINGO:
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
                f">= {_RISK_OFF_OVERRIDE_DIST_MIN:.2f}, "
                f"chainlink={cl_direction} + tiingo aligns with trade={direction}"
            ),
        )
    )
    return True


def _skip(reason: str, gate_results: Optional[list[dict]] = None) -> StrategyDecision:
    return StrategyDecision.skip(
        reason=reason,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        metadata=DecisionMetadata(extras={"gate_results": gate_results or []}),
    )


def evaluate_polymarket_v2(surface: "FullDataSurface") -> Optional[StrategyDecision]:
    """Full V4 fusion evaluation ported from V4FusionStrategy.

    Returns StrategyDecision if handled, None to fall through to gates.
    """
    # Check if we have polymarket outcome data
    if surface.poly_direction is not None:
        return _evaluate_poly_v2(surface)

    # Check for legacy polymarket path
    if surface.v4_recommended_side in ("UP", "DOWN"):
        return _evaluate_poly_legacy(surface)

    # Legacy margin-engine path
    return _evaluate_legacy(surface)


def _evaluate_poly_v2(surface: "FullDataSurface") -> StrategyDecision:
    """Polymarket v2 evaluation -- clean venue-specific recommendation.

    Timing gates:
      - early (>T-180): hard skip
      - optimal (T-30 to T-180): trade if confidence + trade_advised pass
      - late_window (T-5 to T-30): trade only if CLOB divergence >= 4pp
      - expired (<T-5): hard skip
    """
    direction = surface.poly_direction
    trade_advised = surface.poly_trade_advised or False
    confidence = surface.poly_confidence or 0.5
    distance = surface.poly_confidence_distance or abs(confidence - 0.5)
    reason = surface.poly_reason or "unknown"
    timing = surface.poly_timing or "unknown"
    max_entry = surface.poly_max_entry_price
    gates: list[dict] = []

    # v4.4.0: CALM regime skip (audit task #225). v2 model is 45.6% accurate
    # (below coin flip) in VPIN regime=CALM. Skip unconditionally unless env
    # override. See project_v9_analysis.md.
    vpin_regime = surface.regime
    if _FUSION_SKIP_CALM and vpin_regime == "CALM":
        gates.append(
            _gate(
                "calm_regime",
                False,
                "regime=CALM v2 model 45.6% accuracy, skip",
            )
        )
        return _skip("calm_regime_model_underperforms", gates)
    if not _FUSION_SKIP_CALM and vpin_regime == "CALM":
        gates.append(
            _gate(
                "calm_regime",
                True,
                "calm_gate_disabled_by_env (regime=CALM)",
            )
        )
    elif vpin_regime is not None:
        gates.append(
            _gate("calm_regime", True, f"regime={vpin_regime} tradeable")
        )

    # v4.5.0: Feature staleness skip (NULL-proxy, audit #234). 7d data 2026-
    # 04-17: 21.67% of BTC 5m decisions had chainlink OR tiingo NULL; WR on
    # those rows 54.8% vs clean 60.1% = 5.3pp worse. None = stale/missing for
    # gate purposes. Evaluated BEFORE expensive gates.
    if _FUSION_SKIP_STALE_SOURCES:
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

    # Hard skip if < _MIN_OFFSET_SEC left (v4.3.0: default T-45, was T-70).
    # Real fill latency is ~600ms FOK + same-region network; T-45 gives ~42s
    # safety margin. Override via V4_MIN_OFFSET_SEC env var.
    offset = surface.eval_offset or 0
    if offset < _MIN_OFFSET_SEC:
        gates.append(
            _gate(
                "execution_timing",
                False,
                f"T-{offset} < T-{_MIN_OFFSET_SEC} live minimum",
            )
        )
        return _skip(
            f"polymarket: timing={timing} T-{offset} -- too late "
            f"(<{_MIN_OFFSET_SEC}s), skip",
            gates,
        )

    # Expired / too early
    if timing in ("expired", "early"):
        gates.append(_gate("timing", False, f"timing={timing} outside trade window"))
        return _skip(f"polymarket: timing={timing} -- outside window", gates)

    # Late window: only trade with CLOB divergence >= 4pp
    if timing == "late_window":
        clob_implied = surface.clob_implied_up
        if clob_implied is not None:
            # Sequoia's edge over CLOB: how much further from 0.5 is our model vs market
            divergence = distance - abs(float(clob_implied) - 0.5)
            if divergence < 0.04:
                gates.append(
                    _gate(
                        "late_window_divergence", False, f"div={divergence:.3f} < 0.04"
                    )
                )
                return _skip(
                    f"polymarket: late_window but CLOB already priced (div={divergence:.3f} < 0.04)",
                    gates,
                )
        else:
            gates.append(_gate("late_window_divergence", False, "no CLOB data"))
            return _skip("polymarket: late_window but no CLOB data", gates)

    # Legacy "late" label
    if timing == "late":
        gates.append(_gate("timing", False, "timing=late outside window"))
        return _skip(f"polymarket: timing=late -- outside window", gates)

    # Confidence gate
    if distance < 0.12:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < 0.12"))
        return _skip(
            f"polymarket: p_up={confidence:.3f} dist={distance:.3f} < 0.12 threshold",
            gates,
        )

    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= 0.12"))

    risk_off_overridden = False
    if not trade_advised:
        # v4.3.0: direction-aware risk_off override. Sister repo vetoes ALL
        # trades on regime=risk_off even when HIGH-conviction + oracle-aligned;
        # this allows engine to proceed when preconditions in
        # _try_risk_off_override() are met.
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
            _gate("trade_advised", True, "trade_advised=false but risk_off_override applied")
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

    # Chainlink oracle agreement gate (5m Polymarket resolves on Chainlink)
    # If Chainlink delta is available and disagrees with trade direction, skip.
    # This prevents trades where the resolution oracle points the other way.
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

    if surface.delta_chainlink is not None:
        gates.append(_gate("chainlink_agreement", True, "Chainlink agrees"))

    # v4.4.0: Tiingo agreement gate (audit task #228). Mirrors the chainlink
    # gate but on the Tiingo top-of-book 5m delta. If direction disagrees,
    # skip. Missing Tiingo => pass (don't penalise when source missing).
    if not _FUSION_REQUIRE_TIINGO_AGREE:
        gates.append(
            _gate(
                "tiingo_agreement",
                True,
                "tiingo_gate_disabled_by_env",
            )
        )
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

    # v4.5.0: HealthBadge skip gate (audit task #223). Default "degraded"
    # because today's 11:57 UTC LOSS fired at DEGRADED [sources:unknown].
    # "off"      — never skip on health
    # "unsafe"   — skip only UNSAFE
    # "degraded" — skip DEGRADED or UNSAFE (strict)
    if _FUSION_HEALTH_GATE != "off":
        health = _compute_health_badge(
            surface, distance, direction, risk_off_overridden,
        )
        block_on = {
            "unsafe": {HealthStatus.UNSAFE},
            "degraded": {HealthStatus.DEGRADED, HealthStatus.UNSAFE},
        }.get(_FUSION_HEALTH_GATE, set())
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

    entry_reason_prefix = "polymarket_override_" if risk_off_overridden else "polymarket_"
    # Canonical shared fields: regime from v4_regime (HMM classifier),
    # conviction from v4_conviction. v4_regime key no longer appears in
    # extras — it's on the VO as `regime`, so PR #254's trade_recorder
    # fallback is no longer necessary for new writes.
    return StrategyDecision.trade(
        direction=direction,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"{entry_reason_prefix}{reason}_T{surface.eval_offset}",
        metadata=DecisionMetadata(
            regime=surface.v4_regime,
            conviction=surface.v4_conviction,
            window_ts=getattr(surface, "window_ts", None),
            extras={
                "gate_results": gates,
                "poly_direction": direction,
                "poly_confidence_distance": distance,
                "poly_timing": timing,
                "vpin_regime": surface.regime,
                "chainlink_delta": surface.delta_chainlink,
                "chainlink_agrees": True,
                "tiingo_delta": surface.delta_tiingo,
                "tiingo_agrees": True,
                "risk_off_overridden": risk_off_overridden,
            },
        ),
        confidence=surface.v4_conviction or f"dist_{distance:.2f}",
        confidence_score=distance * 2.0,
        entry_cap=max_entry,
        collateral_pct=surface.v4_recommended_collateral_pct,
    )


def _evaluate_poly_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy polymarket path for old timesfm builds."""
    p_up = surface.v2_probability_up or 0.5
    distance = abs(p_up - 0.5)
    direction = surface.v4_recommended_side or ("UP" if p_up > 0.5 else "DOWN")
    gates: list[dict] = []

    if distance < 0.12:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < 0.12"))
        return _skip(f"polymarket_legacy: dist={distance:.3f} < 0.12", gates)

    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= 0.12"))

    # Macro gate
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

    return StrategyDecision.trade(
        direction=direction,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"polymarket_legacy_dist{distance:.2f}_T{surface.eval_offset}",
        metadata=DecisionMetadata(
            regime=surface.v4_regime,
            conviction=surface.v4_conviction,
            window_ts=getattr(surface, "window_ts", None),
            extras={
                "gate_results": gates,
                "v2_probability_up": p_up,
            },
        ),
        confidence=surface.v4_conviction,
        confidence_score=surface.v4_conviction_score or (distance * 2.0),
        entry_cap=surface.poly_max_entry_price,
        collateral_pct=collateral_pct,
    )


def _evaluate_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy margin-engine path (non-polymarket templates)."""
    # Gate 1: Regime
    regime = surface.v4_regime
    gates: list[dict] = []
    if regime and regime not in _TRADEABLE_REGIMES:
        gates.append(_gate("regime", False, f"regime={regime} not tradeable"))
        return _skip(f"regime={regime} not tradeable", gates)

    gates.append(_gate("regime", True, f"regime={regime} tradeable"))

    # Gate 2: Consensus
    if not surface.v4_consensus_safe_to_trade:
        gates.append(_gate("consensus", False, "consensus not safe_to_trade"))
        return _skip("consensus not safe_to_trade", gates)

    gates.append(_gate("consensus", True, "safe_to_trade=true"))

    # Gate 3: Conviction threshold
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

    # Gate 4: Direction
    direction = surface.v4_recommended_side
    if direction is None:
        direction = "UP" if p_up > 0.5 else "DOWN"

    # Gate 5: Macro
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

    return StrategyDecision.trade(
        direction=direction,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"v4_{conviction}_{regime}_T{surface.eval_offset}_p{p_up:.2f}",
        metadata=DecisionMetadata(
            regime=regime,
            conviction=conviction,
            window_ts=getattr(surface, "window_ts", None),
            extras={
                "gate_results": gates,
                "v2_probability_up": p_up,
            },
        ),
        confidence=conviction,
        confidence_score=surface.v4_conviction_score,
        entry_cap=None,
        collateral_pct=collateral_pct,
    )
