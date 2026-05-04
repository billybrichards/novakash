"""Custom hooks for v7_15m_sniper strategy.

15-minute Polymarket BTC sniper — GHOST mode. Fork of v6_sniper adapted
for 15m windows with classifier-only signal (no LGB for 15m).

Primary signal: ``surface.probability_classifier`` (Path 1 classifier
head from timesfm-repo). There is no 15m LGB model, so the v6
ensemble blend (``surface.poly_confidence``) is NOT used. Conviction
is determined solely by |probability_classifier - 0.5|.

Conviction buckets (simplified from v6's LGB+Path1 scheme):

    classifier_strong   |p_classifier - 0.5| >= 0.30 (= p >= 0.80 or
                         p <= 0.20). 83.9% accuracy at T-400 from eval
                         simulation on 15m windows.
    pegged_classifier    p_classifier >= 0.90 OR p_classifier <= 0.10.
                         Near-pegged extreme confidence.

Everything else (mid_conf, no_eval) is blocked.

Entry window: T-400 to T-300 (seconds before 15m window close).
This is the sweet spot from classifier evaluation — accuracy peaks
at T-400 and degrades outside the T-400 to T-300 band.

Defensive gates inherited from v6 (all kept):
    - feature staleness (chainlink + tiingo present)
    - UTC hour-of-day block
    - timing window
    - VPIN floor
    - source agreement (with high-VPIN bypass)
    - trade_advised (with bucket-aware risk_off override)
    - health badge
    - regime gate

Window dedup uses 15m window_ts.
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

_STRATEGY_ID = "v7_15m_sniper"
_VERSION = "7.0.0"


# ── Consecutive-tick entry confirmation state ─────────────────────────────
# Module-level counter keyed by (strategy_id, asset, window_ts). Mirrors the
# pattern used by v8_champion_lgb_only / v9_ensemble (see PR #480 and audit
# note 2026-05-04). Each evaluation increments the count when ALL gates pass;
# any gate failure resets the count to 0 for that key. The hook only emits
# TRADE once count >= ``min_consecutive_pass_ticks``.
#
# The hook is shared across v7_15m_sniper{,_eth,_sol,_xrp} — each child
# strategy evaluates only its own asset (registry asset filter), so
# ``(strategy_id, asset, window_ts)`` is unambiguous for our purposes.
_TICK_COUNTERS: dict[tuple[str, str, int], int] = {}


# ── Tunable knobs (YAML gate_params → env fallback → default) ──────────────
def _min_offset_sec() -> int:
    return _gp.get_int("min_offset_sec", "V7_15M_SNIPER_MIN_OFFSET_SEC", 300)


def _max_offset_sec() -> int:
    return _gp.get_int("max_offset_sec", "V7_15M_SNIPER_MAX_OFFSET_SEC", 400)


def _bucket_abs_dist_strong() -> float:
    """Minimum |p_classifier - 0.5| for classifier_strong bucket.
    Set to 0.30 for 15m — below this the classifier is under 70% accuracy.
    """
    return _gp.get_float(
        "bucket_abs_dist_strong", "V7_15M_SNIPER_BUCKET_ABS_DIST_STRONG", 0.30
    )


def _bucket_path1_extreme_high() -> float:
    return _gp.get_float(
        "bucket_path1_extreme_high", "V7_15M_SNIPER_PATH1_EXTREME_HIGH", 0.90
    )


def _bucket_path1_extreme_low() -> float:
    return _gp.get_float(
        "bucket_path1_extreme_low", "V7_15M_SNIPER_PATH1_EXTREME_LOW", 0.10
    )


def _path1_max_age_s() -> int:
    """120s for 15m — wider than v6's 90s because polls are every ~30s."""
    return _gp.get_int("path1_max_age_s", "V7_15M_SNIPER_PATH1_MAX_AGE_S", 120)


def _path1_skip_on_null() -> bool:
    return _gp.get_bool(
        "path1_skip_on_null", "V7_15M_SNIPER_PATH1_SKIP_ON_NULL", True
    )


def _vpin_min() -> float:
    return _gp.get_float("vpin_min", "V7_15M_SNIPER_VPIN_MIN", 0.45)


def _require_chainlink() -> bool:
    return _gp.get_bool(
        "source_agreement_require_chainlink",
        "V7_15M_SNIPER_REQUIRE_CHAINLINK",
        True,
    )


def _require_tiingo() -> bool:
    return _gp.get_bool(
        "source_agreement_require_tiingo",
        "V7_15M_SNIPER_REQUIRE_TIINGO",
        True,
    )


def _health_gate() -> str:
    return _gp.get_str("health_gate", "V7_15M_SNIPER_HEALTH_GATE", "degraded").lower()


def _skip_stale_sources() -> bool:
    return _gp.get_bool("skip_stale_sources", "V7_15M_SNIPER_SKIP_STALE", True)


def _blocked_utc_hours() -> list[int]:
    return _gp.get_int_list("blocked_utc_hours", "V7_15M_SNIPER_BLOCKED_HOURS", [])


def _tradeable_v4_regimes() -> list[str]:
    return _gp.get_str_list(
        "tradeable_v4_regimes",
        "V7_15M_SNIPER_TRADEABLE_REGIMES",
        ["calm_trend", "volatile_trend", "risk_off"],
    )


def _skip_on_oracle_disagree() -> bool:
    """When False, oracle direction disagreement is logged but does not skip.
    Default False for 15m — let classifier conviction carry the load.
    """
    return _gp.get_bool(
        "skip_on_oracle_disagree",
        "V7_15M_SNIPER_SKIP_ON_ORACLE_DISAGREE",
        False,
    )


def _entry_cap_override() -> Optional[float]:
    """0.80 default for 15m — slightly more conservative than v6's 0.85
    due to wider spreads on less liquid 15m markets.
    """
    v = _gp.get_float("entry_cap_override", "V7_15M_SNIPER_ENTRY_CAP_OVERRIDE", 0.0)
    return v if v > 0 else None


# ── v7 bucket-aware risk_off override ────────────────────────────────────
def _v7_risk_off_override_enabled() -> bool:
    return _gp.get_bool(
        "v7_risk_off_override_enabled",
        "V7_15M_SNIPER_RISK_OFF_OVERRIDE_ENABLED",
        True,
    )


def _v7_risk_off_override_buckets() -> set[str]:
    return set(
        _gp.get_str_list(
            "v7_risk_off_override_buckets",
            "V7_15M_SNIPER_RISK_OFF_OVERRIDE_BUCKETS",
            ["classifier_strong", "pegged_classifier"],
        )
    )


# ── High-VPIN source-agreement bypass ────────────────────────────────────
def _high_vpin_bypass_enabled() -> bool:
    return _gp.get_bool(
        "high_vpin_bypass_enabled",
        "V7_15M_SNIPER_HIGH_VPIN_BYPASS_ENABLED",
        True,
    )


def _high_vpin_bypass_threshold() -> float:
    return _gp.get_float(
        "high_vpin_bypass_threshold",
        "V7_15M_SNIPER_HIGH_VPIN_BYPASS_THRESHOLD",
        0.60,
    )


def _high_vpin_bypass_buckets() -> set[str]:
    return set(
        _gp.get_str_list(
            "high_vpin_bypass_buckets",
            "V7_15M_SNIPER_HIGH_VPIN_BYPASS_BUCKETS",
            ["classifier_strong", "pegged_classifier"],
        )
    )


# ── Consecutive-tick entry confirmation knob ────────────────────────────
def _min_consecutive_pass_ticks() -> int:
    """Minimum consecutive evaluations with all gates passing before TRADE.

    Default 3 — mirrors v8_champion_lgb_only / v9_ensemble. 0 disables
    confirmation (fire on first pass, legacy v7 behaviour).
    """
    return _gp.get_int(
        "min_consecutive_pass_ticks",
        "V7_15M_SNIPER_MIN_TICKS",
        3,
    )


def _try_bucket_risk_off_override(
    reason: str,
    bucket: str,
    direction: "Optional[str]",
    surface: "FullDataSurface",
    gates: list[dict],
) -> bool:
    """Bucket-gated risk_off override for v7.

    Returns True if sister-repo's risk_off veto should be bypassed.
    Preconditions:
      1. v7_risk_off_override_enabled = True
      2. reason contains "risk_off"
      3. bucket in v7_risk_off_override_buckets
      4. direction is not None
      5. chainlink delta aligns with trade direction
    """
    if not _v7_risk_off_override_enabled():
        return False
    if "risk_off" not in (reason or ""):
        return False
    allowed = _v7_risk_off_override_buckets()
    if bucket not in allowed:
        gates.append(_gate("v7_risk_off_override", False,
                           f"bucket={bucket} not in {sorted(allowed)}"))
        return False
    if direction is None:
        return False
    cl_delta = surface.delta_chainlink
    if cl_delta is None:
        gates.append(_gate("v7_risk_off_override", False,
                           "no chainlink delta to verify direction alignment"))
        return False
    cl_direction = "UP" if cl_delta > 0 else "DOWN"
    if cl_direction != direction:
        gates.append(_gate("v7_risk_off_override", False,
                           f"chainlink={cl_direction} disagrees with trade={direction}"))
        return False
    gates.append(_gate(
        "v7_risk_off_override", True,
        f"risk_off overridden: bucket={bucket}, chainlink={cl_direction} aligns with trade={direction}",
    ))
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


# ── Tick-confirmation counter helpers ───────────────────────────────────
def _tick_key(asset: Optional[str], window_ts: Optional[int]) -> tuple[str, str, int]:
    """Build the counter key. Falls back to safe defaults so a missing
    field never raises — worst case is a single shared key that gets
    pruned on the next window.
    """
    return (
        _STRATEGY_ID,
        (asset or "UNKNOWN").upper(),
        int(window_ts or 0),
    )


def _prune_old_tick_counters(current_window_ts: Optional[int]) -> None:
    """Drop counter entries from previous windows.

    Conservative: only prune entries with a STRICTLY smaller window_ts
    than the current one. Entries with unknown/zero window_ts are kept
    until they age out the same way (they will mismatch the live key
    and never grow).
    """
    if not current_window_ts:
        return
    cutoff = int(current_window_ts)
    stale = [k for k in _TICK_COUNTERS if 0 < k[2] < cutoff]
    for k in stale:
        _TICK_COUNTERS.pop(k, None)


def _reset_tick_counter(key: tuple[str, str, int]) -> None:
    _TICK_COUNTERS.pop(key, None)


def _increment_tick_counter(key: tuple[str, str, int]) -> int:
    count = _TICK_COUNTERS.get(key, 0) + 1
    _TICK_COUNTERS[key] = count
    return count


def _get_tick_count(key: tuple[str, str, int]) -> int:
    """Test/inspection helper — current count for a key (0 if absent)."""
    return _TICK_COUNTERS.get(key, 0)


def _reset_all_tick_counters() -> None:
    """Test helper — clear all confirmation state."""
    _TICK_COUNTERS.clear()


def _skip_and_reset(
    reason: str,
    gates: list[dict],
    *,
    extras: Optional[dict] = None,
    tick_key: Optional[tuple[str, str, int]] = None,
) -> StrategyDecision:
    """SKIP wrapper that also resets the tick counter for ``tick_key``.

    Any gate-fail SKIP must reset the counter so the next pass starts
    from 1, not from where the previous run left off.
    """
    if tick_key is not None:
        _reset_tick_counter(tick_key)
    return _skip(reason, gates, extras=extras)


def _path1_age_s(
    surface: "FullDataSurface",
) -> tuple[Optional[float], str]:
    """Age of the path1 classifier reading + which field it came from.

    Preferred source: ``surface.probability_classifier_inferred_at``.
    Fallback: ``surface.assembled_at``.
    Returns (age_seconds, source_label).
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
    """Hour-of-day (UTC) for the window."""
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
    p_classifier: Optional[float],
) -> str:
    """Return the conviction bucket label for this 15m classifier signal.

    Simplified from v6's _classify_bucket — no LGB dimension.

    Labels:
        classifier_strong   |p_classifier - 0.5| >= bucket_abs_dist_strong (0.30)
        pegged_classifier    p_classifier >= 0.90 OR p_classifier <= 0.10
        mid_conf             everything else below the strong threshold
        no_eval              classifier unavailable
    """
    if p_classifier is None:
        return "no_eval"

    high = _bucket_path1_extreme_high()
    low = _bucket_path1_extreme_low()
    strong_thr = _bucket_abs_dist_strong()
    dist = abs(p_classifier - 0.5)

    # pegged_classifier — checked first (narrower, higher conviction).
    if p_classifier >= high or p_classifier <= low:
        return "pegged_classifier"

    # classifier_strong — |p - 0.5| >= threshold.
    if dist >= strong_thr:
        return "classifier_strong"

    # Everything below the threshold is mid_conf (blocked).
    return "mid_conf"


# ── Main entry ─────────────────────────────────────────────────────────────
def evaluate_polymarket_15m_sniper(
    surface: "FullDataSurface",
) -> Optional[StrategyDecision]:
    """Pre-gate hook for v7_15m_sniper.

    Returns a concrete StrategyDecision (SKIP or TRADE). YAML has
    ``gates: []`` so None effectively means 'no decision'.
    """
    gates: list[dict] = []

    # ── Tick-confirmation key + cleanup ──────────────────────────────────
    # Build the per-(strategy, asset, window) counter key once. Every
    # gate-failure SKIP path below routes through ``_skip`` (rebound here
    # to the reset-aware wrapper) so the counter is zeroed on any failure;
    # the final TRADE path increments it and only fires once it reaches
    # the configured threshold.
    _surface_window_ts = getattr(surface, "window_ts", None)
    _surface_asset = getattr(surface, "asset", None)
    tick_key = _tick_key(_surface_asset, _surface_window_ts)
    _prune_old_tick_counters(_surface_window_ts)

    # Locally rebind ``_skip`` so every existing ``return _skip(...)`` in
    # this function automatically resets the tick counter on gate failure.
    # This keeps the patch minimal and avoids touching every skip site.
    def _skip(reason, gates, *, extras=None):  # type: ignore[no-redef]
        return _skip_and_reset(reason, gates, extras=extras, tick_key=tick_key)

    # ── Source staleness (chainlink + tiingo present) ────────────────────
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

    # ── UTC hour-of-day block ────────────────────────────────────────────
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

    # ── Execution timing window (T-400 to T-300 for 15m) ────────────────
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

    # ── VPIN floor ───────────────────────────────────────────────────────
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

    # ── Classifier signal (v7 uses probability_classifier directly) ─────
    p_classifier = getattr(surface, "probability_classifier", None)

    # ── Classifier freshness ────────────────────────────────────────────
    if _path1_skip_on_null() and p_classifier is None:
        gates.append(
            _gate(
                "classifier_freshness",
                False,
                "probability_classifier is None",
            )
        )
        return _skip(
            "no_eval_blocked: classifier NULL",
            gates,
            extras={
                "conviction_bucket": "no_eval_blocked",
                "probability_classifier": p_classifier,
            },
        )
    age_s, age_source = _path1_age_s(surface)
    max_age = _path1_max_age_s()
    if age_source != "inferred_at":
        gates.append(
            _gate(
                "classifier_age_source",
                age_source == "assembled_at_fallback",
                f"age_source={age_source}",
            )
        )
    if age_s is not None and age_s > max_age:
        gates.append(
            _gate(
                "classifier_freshness",
                False,
                f"age={age_s:.1f}s > {max_age}s (src={age_source})",
            )
        )
        return _skip(
            f"no_eval_blocked: classifier stale ({age_s:.1f}s > {max_age}s)",
            gates,
            extras={
                "conviction_bucket": "no_eval_blocked",
                "probability_classifier": p_classifier,
                "classifier_age_source": age_source,
                "classifier_age_s": age_s,
            },
        )
    gates.append(
        _gate(
            "classifier_freshness",
            True,
            (
                f"p_classifier={p_classifier} "
                f"age={'?' if age_s is None else f'{age_s:.1f}s'} "
                f"src={age_source}"
            ),
        )
    )

    # ── Direction resolution ────────────────────────────────────────────
    # Derive direction from the classifier probability.
    # poly_direction may still be set from the surface, but for v7
    # the classifier IS the signal, so we derive from it directly.
    if p_classifier is not None:
        direction = "UP" if p_classifier > 0.5 else "DOWN"
    else:
        direction = surface.poly_direction
        if direction not in ("UP", "DOWN"):
            direction = None

    # ── Classifier-only mode (no sister TimesFM model for this asset) ────
    # BTC has a full TimesFM stack — classifier + LGB + polymarket outcome
    # block (direction, timing, trade_advised, etc). ETH/SOL/XRP serve
    # ``status=no_model`` snapshots (2026-04-20) where only the classifier
    # head is populated; the entire poly block is absent. Detect that case
    # here so the downstream poly_trade_advised / timing / direction gates
    # don't reject every non-BTC eval.
    #
    # Signal: poly_trade_advised, poly_timing and poly_direction are all
    # None — i.e. the poly block was missing, not merely negative. (A
    # negative trade_advised with a real reason is a different skip path.)
    classifier_only_mode = (
        surface.poly_trade_advised is None
        and surface.poly_timing is None
        and surface.poly_direction is None
    )
    if classifier_only_mode:
        gates.append(_gate(
            "classifier_only_mode", True,
            "sister no_model; using p_classifier directly",
        ))

    # ── Conviction bucket (classifier-only, no LGB) ─────────────────────
    bucket = _classify_bucket(p_classifier)
    bucket_extras = {
        "conviction_bucket": bucket,
        "probability_classifier": p_classifier,
        "classifier_distance": abs(p_classifier - 0.5) if p_classifier is not None else None,
    }

    # ── Source agreement (moved below bucket for high-VPIN bypass) ───────
    cl = surface.delta_chainlink
    ti = surface.delta_tiingo
    if cl is not None and ti is not None:
        cl_dir = "UP" if cl > 0 else ("DOWN" if cl < 0 else None)
        ti_dir = "UP" if ti > 0 else ("DOWN" if ti < 0 else None)
        if cl_dir is None or ti_dir is None or cl_dir != ti_dir:
            vpin_val = surface.vpin or 0.0
            if (
                _high_vpin_bypass_enabled()
                and bucket in _high_vpin_bypass_buckets()
                and vpin_val >= _high_vpin_bypass_threshold()
            ):
                gates.append(_gate(
                    "source_agreement_high_vpin_bypass", True,
                    f"bypassed: bucket={bucket}, vpin={vpin_val:.3f} "
                    f">= {_high_vpin_bypass_threshold():.2f}, "
                    f"chainlink={cl_dir} tiingo={ti_dir}",
                ))
                bucket_extras["source_agreement_bypassed"] = True
            else:
                gates.append(_gate(
                    "source_agreement", False,
                    f"chainlink={cl_dir} tiingo={ti_dir}",
                ))
                return _skip(
                    f"source_disagreement: chainlink={cl_dir} tiingo={ti_dir}",
                    gates,
                    extras=bucket_extras,
                )
        else:
            gates.append(_gate("source_agreement", True, f"both agree {cl_dir}"))

    # ── trade_advised (bucket-aware risk_off override) ──────────────────
    # Skipped entirely in classifier_only_mode: the sister model is
    # no_model for this asset, so there is no poly_trade_advised signal
    # to consult. The classifier conviction + downstream oracle-agreement
    # gates carry the load instead.
    if classifier_only_mode:
        gates.append(_gate(
            "trade_advised", True,
            "skipped (classifier_only_mode — no poly advice available)",
        ))
    elif not (surface.poly_trade_advised or False):
        reason = surface.poly_reason or "no_poly_advice"
        if _try_bucket_risk_off_override(reason, bucket, direction, surface, gates):
            gates.append(_gate(
                "trade_advised", True,
                f"trade_advised=false ({reason}) but v7_risk_off_override applied",
            ))
            bucket_extras["risk_off_override_fired"] = True
        else:
            gates.append(_gate("trade_advised", False, reason))
            return _skip(
                f"trade_not_advised: {reason}",
                gates,
                extras=bucket_extras,
            )
    else:
        gates.append(_gate("trade_advised", True, "trade_advised=true"))

    # ── Conviction bucket gate ──────────────────────────────────────────
    if bucket == "classifier_strong":
        gates.append(
            _gate("conviction_bucket", True, "classifier_strong")
        )
    elif bucket == "pegged_classifier":
        gates.append(
            _gate("conviction_bucket", True, "pegged_classifier")
        )
    elif bucket == "mid_conf":
        gates.append(
            _gate(
                "conviction_bucket",
                False,
                f"mid_conf blocked: |p-0.5|={abs(p_classifier - 0.5):.3f} "
                f"< {_bucket_abs_dist_strong():.2f}",
            )
        )
        bucket_extras["conviction_bucket"] = "mid_conf_blocked"
        return _skip(
            f"mid_conf_blocked: classifier conviction too low "
            f"(|p-0.5|={abs(p_classifier - 0.5):.3f} < {_bucket_abs_dist_strong():.2f})",
            gates,
            extras=bucket_extras,
        )
    else:  # no_eval
        gates.append(
            _gate(
                "conviction_bucket",
                False,
                "no_eval (classifier unavailable)",
            )
        )
        bucket_extras["conviction_bucket"] = "no_eval_blocked"
        return _skip("no_eval_blocked: classifier unavailable", gates, extras=bucket_extras)

    # ── Oracle direction agreement with trade ───────────────────────────
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

    # ── Health badge ────────────────────────────────────────────────────
    # Skipped in classifier_only_mode: the badge's eval_band_in_optimal
    # input is derived from poly_timing which is None by construction for
    # no_model assets, and v4_conviction is also None, so the badge would
    # always score UNSAFE. Sources + VPIN + classifier distance are checked
    # independently above.
    health_gate = _health_gate()
    distance = abs(p_classifier - 0.5) if p_classifier is not None else 0.0
    if classifier_only_mode and health_gate != "off":
        gates.append(_gate(
            "health_badge", True,
            "skipped (classifier_only_mode)",
        ))
    elif health_gate != "off":
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

    # ── Regime gate ─────────────────────────────────────────────────────
    tradeable = set(_tradeable_v4_regimes())
    v4_regime = surface.v4_regime
    if v4_regime is not None and v4_regime not in tradeable:
        gates.append(
            _gate("regime", False, f"regime={v4_regime} not tradeable")
        )
        return _skip(f"regime_not_tradeable: {v4_regime}", gates, extras=bucket_extras)
    if v4_regime:
        gates.append(_gate("regime", True, f"regime={v4_regime} tradeable"))

    # ── Consecutive-tick entry confirmation ─────────────────────────────
    # All gates passed. Increment the counter and only emit TRADE once
    # we have ``min_consecutive_pass_ticks`` consecutive passing evals
    # for this (strategy, asset, window) key. Mirrors the
    # ``consecutive_pass_ticks`` gate used by other LIVE strategies
    # (v8_champion_lgb_only, v9_ensemble, v10/v12 LGB, etc).
    required = _min_consecutive_pass_ticks()
    count = _increment_tick_counter(tick_key)
    if required > 0 and count < required:
        gates.append(
            _gate(
                "entry_confirmation",
                False,
                f"{count}/{required} consecutive pass ticks",
            )
        )
        # NOTE: don't reset — we WANT the counter to grow across ticks.
        # Use the raw module-level _skip so the local reset wrapper above
        # doesn't zero our progress.
        return _skip_and_reset(
            f"entry_confirmation: {count}/{required} ticks",
            gates,
            extras=bucket_extras,
            tick_key=None,  # explicit: do NOT reset
        )
    gates.append(
        _gate(
            "entry_confirmation",
            True,
            f"{count}/{required} consecutive pass ticks — confirmed"
            if required > 0
            else "tick confirmation disabled",
        )
    )

    # ── TRADE ───────────────────────────────────────────────────────────
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
            f"v7_15m_sniper_{bucket}_T{surface.eval_offset}_{direction}"
        ),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_timing": surface.poly_timing,
            "v4_regime": v4_regime,
            "vpin_regime": surface.regime,
            "chainlink_delta": cl,
            "tiingo_delta": ti,
            # v7-specific audit trail
            "signal_source": "path1_only",
            "probability_classifier": p_classifier,
            "classifier_distance": distance,
            "conviction_bucket": bucket,
            "classifier_age_s": age_s,
            "classifier_age_source": age_source,
            "timescale": "15m",
            "entry_cap_override": _cap_override,
            "classifier_only_mode": classifier_only_mode,
            "entry_confirmation_count": count,
            "entry_confirmation_required": required,
        },
    )
