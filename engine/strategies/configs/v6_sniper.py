"""Custom hooks for v6_sniper strategy.

Bidirectional ensemble sniper built on the ruins of v5_ensemble (hub
note #182: v5 net-negative on 72h audit). Primary signal is the LGB +
Path1 ensemble blend (surface.poly_confidence); per-window accept/reject
is driven by the conviction buckets isolated in hub note #183:

    agree_strong   both models agree direction + |p_up - 0.5| >= 0.20
                   -> 77.8% WR, +$60 on 7d
    pegged_path1   path1 >= 0.95 OR path1 <= 0.05, LGB not strongly
                   opposing (|p_lgb - 0.5| > 0.10 in opposite dir blocks)
                   -> 100% WR n=6, +$20 on 7d

Everything else (no_eval, mid_conf) is a bleed bucket and we block it.

Fork of v5_ensemble.evaluate_polymarket_ensemble — copies the inherited
v4_fusion gates that still apply (stale sources, health badge, chainlink
/ tiingo agreement, trade_advised) and replaces the 0.12 confidence
cliff with the conviction-bucket gate.

Reads RAW ``poly_confidence`` (the ensemble blend from timesfm) while
bucket thresholds were measured on raw values. Future PR #281 introduces
``surface.probability_up_calibrated`` from the isotonic pass; flip
``prefer_raw_probability: false`` after a fresh 72h shadow re-validates
the bucket boundaries on calibrated data.

Hook-defined gates (no declarative registry entries today; listed in the
YAML's ``gates: []`` section as a roadmap item):
    - ensemble_path1_freshness
    - ensemble_conviction_bucket
    - vpin_min
    - blocked_utc_hours
    - source_agreement (chainlink + tiingo)
    - health_badge (inherited from v4/v5)
    - feature_staleness (inherited from v4/v5)
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.alert_logic import score_signal_health
from domain.alert_values import HealthStatus
from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v6_sniper"
_VERSION = "6.0.5"


# ── Tunable knobs (YAML gate_params → env fallback → default) ──────────────
def _min_offset_sec() -> int:
    # v6.0.2: widened 60 → 30 per Billy for overnight run.
    return _gp.get_int("min_offset_sec", "V6_SNIPER_MIN_OFFSET_SEC", 30)


def _max_offset_sec() -> int:
    # v6.0.2: widened 180 → 240 per Billy for overnight run.
    return _gp.get_int("max_offset_sec", "V6_SNIPER_MAX_OFFSET_SEC", 240)


def _bucket_abs_dist_strong() -> float:
    return _gp.get_float(
        "bucket_abs_dist_strong", "V6_SNIPER_BUCKET_ABS_DIST_STRONG", 0.20
    )


def _bucket_path1_extreme_high() -> float:
    # v6.0.1: relaxed 0.95 → 0.90 per Billy (captures near-pegged winners).
    return _gp.get_float(
        "bucket_path1_extreme_high", "V6_SNIPER_PATH1_EXTREME_HIGH", 0.90
    )


def _bucket_path1_extreme_low() -> float:
    # v6.0.1: relaxed 0.05 → 0.10 per Billy (symmetric DOWN side).
    return _gp.get_float(
        "bucket_path1_extreme_low", "V6_SNIPER_PATH1_EXTREME_LOW", 0.10
    )


def _bucket_lgb_opposite_block() -> float:
    return _gp.get_float(
        "bucket_lgb_opposite_block", "V6_SNIPER_LGB_OPPOSITE_BLOCK", 0.10
    )


def _bucket_block_mid_conf() -> bool:
    return _gp.get_bool(
        "bucket_block_mid_conf", "V6_SNIPER_BLOCK_MID_CONF", True
    )


def _path1_max_age_s() -> int:
    return _gp.get_int("path1_max_age_s", "V6_SNIPER_PATH1_MAX_AGE_S", 30)


def _path1_skip_on_null() -> bool:
    return _gp.get_bool(
        "path1_skip_on_null", "V6_SNIPER_PATH1_SKIP_ON_NULL", True
    )


def _vpin_min() -> float:
    return _gp.get_float("vpin_min", "V6_SNIPER_VPIN_MIN", 0.45)


def _require_chainlink() -> bool:
    return _gp.get_bool(
        "source_agreement_require_chainlink",
        "V6_SNIPER_REQUIRE_CHAINLINK",
        True,
    )


def _require_tiingo() -> bool:
    return _gp.get_bool(
        "source_agreement_require_tiingo",
        "V6_SNIPER_REQUIRE_TIINGO",
        True,
    )


def _health_gate() -> str:
    return _gp.get_str("health_gate", "V6_SNIPER_HEALTH_GATE", "degraded").lower()


def _skip_stale_sources() -> bool:
    return _gp.get_bool("skip_stale_sources", "V6_SNIPER_SKIP_STALE", True)


def _blocked_utc_hours() -> list[int]:
    # v6.0.1: disabled by default per Billy (removed [7,8,9] UTC block).
    # Ops can re-enable via YAML or V6_SNIPER_BLOCKED_HOURS env var.
    return _gp.get_int_list("blocked_utc_hours", "V6_SNIPER_BLOCKED_HOURS", [])


def _tradeable_v4_regimes() -> list[str]:
    # v6.0.1: risk_off added per Billy. chop still blocked.
    return _gp.get_str_list(
        "tradeable_v4_regimes",
        "V6_SNIPER_TRADEABLE_REGIMES",
        ["calm_trend", "volatile_trend", "risk_off"],
    )


def _ensemble_source() -> str:
    return _gp.get_str(
        "ensemble_signal_source", "V6_SNIPER_SIGNAL_SOURCE", "ensemble"
    ).lower()


def _skip_on_ensemble_fallback() -> bool:
    return _gp.get_bool(
        "ensemble_skip_on_fallback", "V6_SNIPER_SKIP_ON_FALLBACK", True
    )


def _prefer_raw_probability() -> bool:
    return _gp.get_bool(
        "prefer_raw_probability", "V6_SNIPER_PREFER_RAW_PROBABILITY", True
    )


def _skip_on_oracle_disagree() -> bool:
    """v6.0.3: Gate for oracle direction disagreement.

    True (default):  skip when chainlink OR tiingo delta-direction
                     is opposite the trade direction.
    False:           log the disagreement as a gate entry but DON'T
                     skip — let v6's conviction buckets + VPIN gate
                     carry the selectivity load on their own.

    Set to False for 2026-04-19 overnight run per Billy — v6's strict
    gates (agree_strong |dist| >= 0.20 OR pegged_path1 0.90/0.10, VPIN
    floor 0.45, feature staleness, health badge) are considered enough
    signal-quality assurance without oracle veto.

    NOTE: still respects ``source_agreement_require_chainlink`` /
    ``source_agreement_require_tiingo`` for NULL / staleness skips —
    those are about feed health, not direction opinion.
    """
    return _gp.get_bool(
        "skip_on_oracle_disagree",
        "V6_SNIPER_SKIP_ON_ORACLE_DISAGREE",
        True,
    )


# ── risk_off override helpers (v6.0.4: ported from v5_ensemble) ───────────
def _risk_off_override_enabled() -> bool:
    """v6.0.4: enables the sister-repo risk_off veto bypass for HIGH-
    conviction + oracle-aligned trades. Mirrors v4_fusion's behaviour.
    YAML: risk_off_override_enabled. Default True.
    """
    return _gp.get_bool(
        "risk_off_override_enabled", "V4_RISK_OFF_OVERRIDE_ENABLED", True
    )


def _risk_off_override_dist_min() -> float:
    """Minimum |p_up - 0.5| required for the override to fire."""
    return _gp.get_float(
        "risk_off_override_dist_min", "V4_RISK_OFF_OVERRIDE_DIST_MIN", 0.20
    )


def _risk_off_override_require_tiingo() -> bool:
    """If True, override requires BOTH chainlink AND tiingo to agree
    with the trade direction. If False, only chainlink alignment is
    needed (more permissive).
    """
    return _gp.get_bool(
        "risk_off_override_require_tiingo",
        "V4_RISK_OFF_OVERRIDE_REQUIRE_TIINGO",
        True,
    )


def _entry_cap_override() -> Optional[float]:
    """v6.0.1: explicit cap override ($0.85 default) — bypasses the
    surface's poly_max_entry_price default (~0.65-0.70). Returns None
    if unset or set to 0, which falls back to the surface cap.

    FAK ladder consumes this as rung 1; rung 2 is cap + pi_bonus (~3c),
    so an 0.85 override translates to FAK attempts at $0.85 and ~$0.88.
    """
    v = _gp.get_float("entry_cap_override", "V6_SNIPER_ENTRY_CAP_OVERRIDE", 0.0)
    return v if v > 0 else None


def _try_risk_off_override(
    reason: str,
    distance: float,
    direction: Optional[str],
    surface: "FullDataSurface",
    gates: list[dict],
) -> bool:
    """v6.0.4: verbatim port of v5_ensemble._try_risk_off_override.

    Returns True if the sister-repo's risk_off veto should be bypassed.
    Preconditions:
      1. risk_off_override_enabled = True
      2. reason contains "risk_off"
      3. distance >= risk_off_override_dist_min (HIGH-conviction only)
      4. direction is not None
      5. chainlink delta aligns with trade direction
      6. if require_tiingo: tiingo delta also aligns
    """
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
                f">= {dist_min:.2f}, chainlink + tiingo align with {direction}"
            ),
        )
    )
    return True


# ── Utility helpers ────────────────────────────────────────────────────────
def _gate(name: str, passed: bool, reason: str) -> dict:
    return {"gate": name, "passed": passed, "reason": reason}


def _skip(reason: str, gates: list[dict], *, extras: Optional[dict] = None) -> StrategyDecision:
    meta: dict = {"gate_results": gates}
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


def _path1_age_s(
    surface: "FullDataSurface",
) -> tuple[Optional[float], str]:
    """Age of the path1 classifier reading + which field it came from.

    Preferred source: ``surface.probability_classifier_inferred_at`` —
    the per-inference wall-clock timestamp the data-surface layer
    populates from the upstream v4 snapshot. Tracks the REAL classifier
    age even when the same cached snapshot feeds many successive evals
    across a 5-minute window.

    Fallback: ``surface.assembled_at`` — wall clock at surface-assembly.
    Over-estimates freshness-loss when a cached snapshot is reused at
    late offsets (which is exactly the false-positive
    ``no_eval_blocked`` pattern this split exists to fix). Still
    shipped as a fallback so older engine builds that haven't yet
    populated ``probability_classifier_inferred_at`` (e.g. during the
    data_surface PR rollout) don't silently bypass the gate.

    Returns (age_seconds, source_label). source_label is one of:
      - ``"inferred_at"``      — used the new per-inference field
      - ``"assembled_at_fallback"`` — field absent, used surface proxy
      - ``"unavailable"``      — neither field usable (e.g. test surface)

    Callers should record ``source_label`` in gate metadata so staleness
    mismatches between old/new surfaces are visible in post-run audits.
    """
    inferred_at = getattr(surface, "probability_classifier_inferred_at", None)
    if inferred_at:
        return max(0.0, time.time() - float(inferred_at)), "inferred_at"
    assembled_at = getattr(surface, "assembled_at", None)
    if not assembled_at:
        return None, "unavailable"
    return (
        max(0.0, time.time() - float(assembled_at)),
        "assembled_at_fallback",
    )


def _window_utc_hour(surface: "FullDataSurface") -> Optional[int]:
    """Hour-of-day (UTC) for the window. Prefers surface.hour_utc, then
    derives from window_ts. Returns None when neither is usable."""
    hour = getattr(surface, "hour_utc", None)
    if hour is not None:
        return int(hour)
    window_ts = getattr(surface, "window_ts", None)
    if window_ts:
        try:
            return _dt.datetime.fromtimestamp(
                int(window_ts), _dt.timezone.utc
            ).hour
        except (ValueError, OSError):
            return None
    return None


def _sources_agree_surface(surface: "FullDataSurface") -> Optional[bool]:
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
):
    confidence_label = surface.v4_conviction or None
    eval_band_in_optimal = surface.poly_timing == "optimal"
    return score_signal_health(
        vpin=surface.vpin,
        p_up=(
            0.5 + distance if direction == "UP"
            else (0.5 - distance if direction == "DOWN" else None)
        ),
        p_up_distance=distance,
        sources_agree=_sources_agree_surface(surface),
        confidence_label=confidence_label,
        confidence_override_active=False,
        eval_band_in_optimal=eval_band_in_optimal,
        chainlink_feed_age_s=None,
    )


def _classify_bucket(
    p_lgb: Optional[float],
    p_path1: Optional[float],
    probability_up: float,
    direction: Optional[str],
) -> str:
    """Return the note #183 conviction bucket label for this surface.

    Labels:
        agree_strong        both models same direction + |p_up - 0.5| >= threshold
        pegged_path1        path1 extreme + LGB not strongly opposing
        mid_conf            0.5 < p < 0.7 (dist < 0.20) — bleed bucket
        no_eval             classifier unavailable / insufficient inputs
    """
    if p_path1 is None:
        return "no_eval"

    dist = abs(probability_up - 0.5)
    high = _bucket_path1_extreme_high()
    low = _bucket_path1_extreme_low()
    opp_block = _bucket_lgb_opposite_block()
    strong_thr = _bucket_abs_dist_strong()

    # pegged_path1 — checked FIRST because it is the narrower accept window.
    if p_path1 >= high or p_path1 <= low:
        path1_dir = "UP" if p_path1 >= high else "DOWN"
        if p_lgb is None:
            # LGB missing on an extreme path1: treat as pegged_path1 accept;
            # downstream consumers will block on path1_skip_on_null if that
            # is a concern for them.
            return "pegged_path1"
        lgb_dist = abs(p_lgb - 0.5)
        lgb_dir = "UP" if p_lgb > 0.5 else "DOWN"
        if lgb_dir != path1_dir and lgb_dist > opp_block:
            # LGB strongly opposes path1 → reject rather than accept.
            return "lgb_blocks_pegged"
        return "pegged_path1"

    # agree_strong — both models same direction + distance >= threshold.
    if p_lgb is not None and direction is not None and dist >= strong_thr:
        lgb_dir = "UP" if p_lgb > 0.5 else "DOWN"
        if lgb_dir == direction:
            return "agree_strong"

    # Everything else is the mid-conf bleed bucket.
    return "mid_conf"


# ── Main entry ─────────────────────────────────────────────────────────────
def evaluate_polymarket_sniper(
    surface: "FullDataSurface",
) -> Optional[StrategyDecision]:
    """Pre-gate hook: returns a StrategyDecision or None (falls through to
    YAML gates — but YAML has ``gates: []`` so None effectively means
    'no decision'; we always return a concrete SKIP or TRADE).
    """
    gates: list[dict] = []

    # ── Source agreement (chainlink + tiingo present + aligned) ───────────
    if _skip_stale_sources():
        missing = []
        if _require_chainlink() and surface.delta_chainlink is None:
            missing.append("chainlink")
        if _require_tiingo() and surface.delta_tiingo is None:
            missing.append("tiingo")
        if missing:
            gates.append(
                _gate(
                    "feature_staleness",
                    False,
                    f"sources_missing: {','.join(missing)}",
                )
            )
            return _skip(
                f"feature_stale: {','.join(missing)} missing at eval", gates
            )
        gates.append(_gate("feature_staleness", True, "chainlink + tiingo present"))

    # ── UTC hour-of-day block ─────────────────────────────────────────────
    blocked = _blocked_utc_hours()
    if blocked:
        hour = _window_utc_hour(surface)
        if hour is not None and hour in blocked:
            gates.append(
                _gate(
                    "blocked_utc_hours",
                    False,
                    f"hour={hour} in blocked {sorted(blocked)}",
                )
            )
            return _skip(f"blocked_utc_hour: hour={hour}", gates)
        gates.append(
            _gate(
                "blocked_utc_hours",
                True,
                f"hour={hour} not in blocked {sorted(blocked)}",
            )
        )

    # ── Execution timing window ───────────────────────────────────────────
    offset = surface.eval_offset if surface.eval_offset is not None else 0
    min_off = _min_offset_sec()
    max_off = _max_offset_sec()
    if offset < min_off:
        gates.append(
            _gate("timing", False, f"T-{offset} < T-{min_off} minimum")
        )
        return _skip(f"timing_too_early: T-{offset} < T-{min_off}", gates)
    if offset > max_off:
        gates.append(
            _gate("timing", False, f"T-{offset} > T-{max_off} maximum")
        )
        return _skip(f"timing_too_late: T-{offset} > T-{max_off}", gates)
    gates.append(
        _gate("timing", True, f"T-{offset} in [T-{min_off}, T-{max_off}]")
    )

    # ── VPIN floor ────────────────────────────────────────────────────────
    vpin = surface.vpin or 0.0
    vpin_floor = _vpin_min()
    if vpin < vpin_floor:
        gates.append(
            _gate("vpin_min", False, f"vpin={vpin:.3f} < {vpin_floor:.3f}")
        )
        return _skip(f"vpin_too_low: vpin={vpin:.3f} < {vpin_floor:.3f}", gates)
    gates.append(
        _gate("vpin_min", True, f"vpin={vpin:.3f} >= {vpin_floor:.3f}")
    )

    # ── Source agreement (directions actually match) ──────────────────────
    cl = surface.delta_chainlink
    ti = surface.delta_tiingo
    if cl is not None and ti is not None:
        cl_dir = "UP" if cl > 0 else ("DOWN" if cl < 0 else None)
        ti_dir = "UP" if ti > 0 else ("DOWN" if ti < 0 else None)
        if cl_dir is None or ti_dir is None or cl_dir != ti_dir:
            gates.append(
                _gate(
                    "source_agreement",
                    False,
                    f"chainlink={cl_dir} tiingo={ti_dir}",
                )
            )
            return _skip(
                f"source_disagreement: chainlink={cl_dir} tiingo={ti_dir}",
                gates,
            )
        gates.append(
            _gate("source_agreement", True, f"both agree {cl_dir}")
        )

    # ── trade_advised ────────────────────────────────────────────────────
    # v6.0.4: if sister-repo veto fires, try risk_off override BEFORE
    # skipping. Uses surface.poly_direction + poly_confidence_distance
    # as the preliminary direction/distance (ensemble bucket logic below
    # will reconcile or reject). Matches v4_fusion / v5_ensemble pattern.
    if not (surface.poly_trade_advised or False):
        reason = surface.poly_reason or "no_poly_advice"
        _prelim_dir = surface.poly_direction or surface.v4_recommended_side
        _prelim_dist = surface.poly_confidence_distance or 0.0
        if _try_risk_off_override(reason, _prelim_dist, _prelim_dir, surface, gates):
            gates.append(
                _gate(
                    "trade_advised",
                    True,
                    f"trade_advised=false ({reason}) but risk_off_override applied",
                )
            )
        else:
            gates.append(_gate("trade_advised", False, reason))
            return _skip(f"trade_not_advised: {reason}", gates)
    else:
        gates.append(_gate("trade_advised", True, "trade_advised=true"))

    # ── Ensemble fields + signal-source selection ────────────────────────
    p_lgb = getattr(surface, "probability_lgb", None)
    p_path1 = getattr(surface, "probability_classifier", None)
    ens_cfg = getattr(surface, "ensemble_config", None)

    if (
        _skip_on_ensemble_fallback()
        and ens_cfg
        and ens_cfg.get("mode") == "fallback_lgb_only"
    ):
        gates.append(
            _gate(
                "ensemble_fallback_sanity",
                False,
                "ensemble degraded to fallback_lgb_only",
            )
        )
        return _skip("ensemble_fallback_lgb_only", gates)

    # Raw vs calibrated selection for probability_up.
    # 2026-04-19: surface.probability_up_calibrated lands with engine-side
    # PR #281 (timesfm isotonic). While not present, getattr returns None
    # and we fall through to the raw poly_confidence reading regardless
    # of the flag — that's the safe behaviour.
    prefer_raw = _prefer_raw_probability()
    p_calibrated = getattr(surface, "probability_up_calibrated", None)
    p_raw = surface.poly_confidence
    if prefer_raw or p_calibrated is None:
        probability_up = p_raw if p_raw is not None else 0.5
        read_source = "raw"
    else:
        probability_up = float(p_calibrated)
        read_source = "calibrated"

    # ── Path1 freshness ─────────────────────────────────────────────────
    if _path1_skip_on_null() and p_path1 is None:
        gates.append(
            _gate(
                "ensemble_path1_freshness",
                False,
                "probability_classifier is None",
            )
        )
        return _skip(
            "no_eval_blocked: path1 classifier NULL",
            gates,
            extras={
                "conviction_bucket": "no_eval_blocked",
                "probability_raw": p_raw,
                "probability_calibrated": p_calibrated,
                "read_probability_source": read_source,
                "lgb": p_lgb,
                "path1": p_path1,
            },
        )
    age_s, age_source = _path1_age_s(surface)
    max_age = _path1_max_age_s()
    # Record the age source in metadata so the fallback-vs-real-field
    # split is visible in strategy_decisions audits. When "assembled_at_fallback"
    # shows up repeatedly after this PR ships, it flags surfaces the new
    # data_surface PR hasn't populated yet (e.g. older engine builds).
    if age_source != "inferred_at":
        gates.append(
            _gate(
                "ensemble_path1_age_source",
                age_source == "assembled_at_fallback",  # True = fallback ok, False = no data
                f"age_source={age_source}",
            )
        )
    if age_s is not None and age_s > max_age:
        gates.append(
            _gate(
                "ensemble_path1_freshness",
                False,
                f"age={age_s:.1f}s > {max_age}s (src={age_source})",
            )
        )
        return _skip(
            f"no_eval_blocked: path1 stale ({age_s:.1f}s > {max_age}s)",
            gates,
            extras={
                "conviction_bucket": "no_eval_blocked",
                "probability_raw": p_raw,
                "probability_calibrated": p_calibrated,
                "read_probability_source": read_source,
                "lgb": p_lgb,
                "path1": p_path1,
                "path1_age_source": age_source,
                "path1_age_s": age_s,
            },
        )
    gates.append(
        _gate(
            "ensemble_path1_freshness",
            True,
            (
                f"path1={p_path1} "
                f"age={'?' if age_s is None else f'{age_s:.1f}s'} "
                f"src={age_source}"
            ),
        )
    )

    # ── Direction resolution ─────────────────────────────────────────────
    direction = surface.poly_direction
    if direction not in ("UP", "DOWN"):
        # Derive from probability when poly_direction absent.
        direction = "UP" if probability_up > 0.5 else "DOWN"

    # ── Conviction bucket ────────────────────────────────────────────────
    bucket = _classify_bucket(p_lgb, p_path1, probability_up, direction)
    bucket_extras = {
        "conviction_bucket": bucket,
        "probability_raw": p_raw,
        "probability_calibrated": p_calibrated,
        "read_probability_source": read_source,
        "lgb": p_lgb,
        "path1": p_path1,
    }

    if bucket == "agree_strong":
        gates.append(
            _gate("ensemble_conviction_bucket", True, "agree_strong")
        )
    elif bucket == "pegged_path1":
        gates.append(
            _gate("ensemble_conviction_bucket", True, "pegged_path1")
        )
    elif bucket == "lgb_blocks_pegged":
        gates.append(
            _gate(
                "ensemble_conviction_bucket",
                False,
                "pegged_path1 but LGB strongly opposes",
            )
        )
        bucket_extras["conviction_bucket"] = "pegged_path1_blocked_by_lgb"
        return _skip(
            "pegged_path1_blocked_by_lgb: LGB opposite direction, "
            f"|dist|>{_bucket_lgb_opposite_block():.2f}",
            gates,
            extras=bucket_extras,
        )
    elif bucket == "mid_conf":
        if _bucket_block_mid_conf():
            gates.append(
                _gate(
                    "ensemble_conviction_bucket",
                    False,
                    "mid_conf bucket blocked",
                )
            )
            bucket_extras["conviction_bucket"] = "mid_conf_blocked"
            return _skip(
                "mid_conf_blocked: neither agree_strong nor pegged_path1",
                gates,
                extras=bucket_extras,
            )
        gates.append(
            _gate(
                "ensemble_conviction_bucket",
                True,
                "mid_conf bucket (block disabled)",
            )
        )
    else:  # no_eval
        gates.append(
            _gate(
                "ensemble_conviction_bucket",
                False,
                "no_eval (classifier unavailable)",
            )
        )
        bucket_extras["conviction_bucket"] = "no_eval_blocked"
        return _skip("no_eval_blocked: insufficient ensemble inputs", gates, extras=bucket_extras)

    # ── Chainlink + Tiingo direction agreement with trade ───────────────
    # v6.0.3: direction-agreement checks are gated on
    # ``skip_on_oracle_disagree``. When False, disagreement is logged
    # as a gate entry but does NOT skip.
    _skip_disagree = _skip_on_oracle_disagree()
    if cl is not None:
        cl_direction = "UP" if cl > 0 else "DOWN"
        if cl_direction != direction:
            gates.append(
                _gate(
                    "chainlink_agreement",
                    False,
                    f"oracle={cl_direction} vs trade={direction}",
                )
            )
            if _skip_disagree:
                return _skip(
                    f"chainlink_disagrees: oracle={cl_direction} vs trade={direction}",
                    gates,
                    extras=bucket_extras,
                )
        else:
            gates.append(_gate("chainlink_agreement", True, "Chainlink agrees"))
    if ti is not None:
        ti_direction = "UP" if ti > 0 else "DOWN"
        if ti_direction != direction:
            gates.append(
                _gate(
                    "tiingo_agreement",
                    False,
                    f"tiingo={ti_direction} vs trade={direction}",
                )
            )
            if _skip_disagree:
                return _skip(
                    f"tiingo_disagrees: tiingo={ti_direction} vs trade={direction}",
                    gates,
                    extras=bucket_extras,
                )
        else:
            gates.append(_gate("tiingo_agreement", True, "Tiingo agrees"))

    # ── Health badge ─────────────────────────────────────────────────────
    health_gate = _health_gate()
    distance = abs(probability_up - 0.5)
    if health_gate != "off":
        health = _compute_health_badge(surface, distance, direction)
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
                extras=bucket_extras,
            )
        gates.append(
            _gate(
                "health_badge",
                True,
                f"status={health.status.value}",
            )
        )

    # ── Regime (skip risk_off + unknown regimes) ─────────────────────────
    tradeable = set(_tradeable_v4_regimes())
    v4_regime = surface.v4_regime
    if v4_regime is not None and v4_regime not in tradeable:
        gates.append(
            _gate("regime", False, f"regime={v4_regime} not tradeable")
        )
        return _skip(f"regime_not_tradeable: {v4_regime}", gates, extras=bucket_extras)
    if v4_regime:
        gates.append(_gate("regime", True, f"regime={v4_regime} tradeable"))

    # ── TRADE ────────────────────────────────────────────────────────────
    # v6.0.1: entry_cap_override (default 0.85) beats surface default.
    _cap_override = _entry_cap_override()
    _entry_cap = _cap_override if _cap_override is not None else surface.poly_max_entry_price
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=surface.v4_conviction or f"dist_{distance:.2f}",
        confidence_score=distance * 2.0,
        entry_cap=_entry_cap,
        collateral_pct=surface.v4_recommended_collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            f"v6_sniper_{bucket}_T{surface.eval_offset}_{direction}"
        ),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": distance,
            "poly_timing": surface.poly_timing,
            "v4_regime": v4_regime,
            "vpin_regime": surface.regime,
            "chainlink_delta": cl,
            "tiingo_delta": ti,
            # v6-specific audit trail
            "signal_source": _ensemble_source(),
            "probability_used": probability_up,
            "probability_raw": p_raw,
            "probability_calibrated": p_calibrated,
            "read_probability_source": read_source,
            "conviction_bucket": bucket,
            "lgb": p_lgb,
            "path1": p_path1,
            "path1_age_s": age_s,
            "path1_age_source": age_source,
            "ensemble_config": ens_cfg,
        },
    )
