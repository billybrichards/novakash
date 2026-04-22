"""Custom hook for v8_champion strategy.

New primary LIVE strategy (2026-04-21). Mirrors v5_ensemble's ensemble-
probability pattern with a purpose-built gate stack derived from Hub
note #209 (v8_agent proposal) plus three refinements:

  1. UP fill-floor (0.55) inherited from v6.1.4 — cheap contrarian UP
     bleeds capital across every strategy observed in the shadow log.
  2. Asymmetric UP/DOWN: DOWN accepts either conviction bucket; UP requires
     BOTH agree_strong AND pegged_path1 (per ``up_require_both_buckets``).
  3. Hour-of-day block on h00 / h05 / h14 UTC — per v8 proposal data.

Gate order (TRADE path):
  1. Timing            — eval_offset in [30, 200]
  2. Hour block        — current_utc_hour not in {0, 5, 14}
  3. Regime            — v4_regime in {volatile_trend, chop}
  4. Source agreement  — chainlink + tiingo both non-null
  5. VPIN guard        — 0.40 <= vpin <= 0.85
  6. Ensemble bucket   — agree_strong OR pegged_path1 (HIGH conv)
  7. Fill-band         — CLOB ask in [0.30, 0.65]
  8. UP asymmetric     — if direction=UP: fill >= 0.55 AND both buckets
  9. Direction present — surface.poly_direction resolvable

Emits TRADE with direction + conviction score, entry_cap 0.80, and
detailed gate_results metadata for the shadow report. SKIP paths
record which gate blocked in ``skip_reason`` + ``gate_results``.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v8_champion"
_VERSION = "8.0.0"
_ENTRY_CAP = 0.80


# ── YAML / env knobs ────────────────────────────────────────────────────────
def _min_offset_sec() -> int:
    return _gp.get_int("min_offset_sec", "V8_MIN_OFFSET_SEC", 30)


def _max_offset_sec() -> int:
    return _gp.get_int("max_offset_sec", "V8_MAX_OFFSET_SEC", 200)


def _tradeable_regimes() -> set[str]:
    return set(
        _gp.get_str_list(
            "tradeable_v4_regimes",
            "V8_TRADEABLE_V4_REGIMES",
            ["volatile_trend", "chop"],
        )
    )


def _bucket_abs_dist_strong() -> float:
    return _gp.get_float(
        "bucket_abs_dist_strong", "V8_BUCKET_ABS_DIST_STRONG", 0.20
    )


def _bucket_path1_extreme_high() -> float:
    return _gp.get_float(
        "bucket_path1_extreme_high", "V8_BUCKET_PATH1_EXTREME_HIGH", 0.90
    )


def _bucket_path1_extreme_low() -> float:
    return _gp.get_float(
        "bucket_path1_extreme_low", "V8_BUCKET_PATH1_EXTREME_LOW", 0.10
    )


def _fill_band_min() -> float:
    return _gp.get_float("fill_band_min", "V8_FILL_BAND_MIN", 0.30)


def _fill_band_max() -> float:
    return _gp.get_float("fill_band_max", "V8_FILL_BAND_MAX", 0.65)


def _up_min_fill_price() -> float:
    return _gp.get_float("up_min_fill_price", "V8_UP_MIN_FILL_PRICE", 0.55)


def _up_require_both_buckets() -> bool:
    return _gp.get_bool(
        "up_require_both_buckets", "V8_UP_REQUIRE_BOTH_BUCKETS", True
    )


def _blocked_utc_hours() -> list[int]:
    return _gp.get_int_list(
        "blocked_utc_hours", "V8_BLOCKED_UTC_HOURS", [0, 5, 14]
    )


def _require_chainlink() -> bool:
    return _gp.get_bool(
        "source_agreement_require_chainlink",
        "V8_REQUIRE_CHAINLINK",
        True,
    )


def _require_tiingo() -> bool:
    return _gp.get_bool(
        "source_agreement_require_tiingo", "V8_REQUIRE_TIINGO", True
    )


def _vpin_min() -> float:
    return _gp.get_float("vpin_min", "V8_VPIN_MIN", 0.40)


def _vpin_max() -> float:
    return _gp.get_float("vpin_max", "V8_VPIN_MAX", 0.85)


# ── Helpers ─────────────────────────────────────────────────────────────────
def _gate(name: str, passed: bool, reason: str) -> dict:
    return {"gate": name, "passed": passed, "reason": reason}


def _skip(
    reason: str,
    gates: list[dict],
    *,
    direction: Optional[str] = None,
    bucket: Optional[str] = None,
    extras: Optional[dict] = None,
) -> StrategyDecision:
    meta: dict = {"gate_results": gates}
    if bucket is not None:
        meta["conviction_bucket"] = bucket
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


def _classify_bucket(
    p_lgb: Optional[float],
    p_path1: Optional[float],
    probability_up: float,
    direction: Optional[str],
) -> tuple[str, bool, bool]:
    """Return (bucket_label, is_agree_strong, is_pegged_path1).

    Mirrors v6_sniper._classify_bucket simplified (no asymmetric thresholds
    inside the bucket calc — v8 handles asymmetry at the gate level).
    """
    if p_path1 is None:
        return "no_eval", False, False

    strong_thr = _bucket_abs_dist_strong()
    high = _bucket_path1_extreme_high()
    low = _bucket_path1_extreme_low()

    dist = abs(probability_up - 0.5)

    is_pegged_path1 = (p_path1 >= high) or (p_path1 <= low)
    is_agree_strong = False
    if p_lgb is not None and direction is not None and dist >= strong_thr:
        lgb_dir = "UP" if p_lgb > 0.5 else "DOWN"
        if lgb_dir == direction:
            is_agree_strong = True

    if is_pegged_path1 and is_agree_strong:
        label = "agree_strong_and_pegged"
    elif is_pegged_path1:
        label = "pegged_path1"
    elif is_agree_strong:
        label = "agree_strong"
    else:
        label = "mid_conf"
    return label, is_agree_strong, is_pegged_path1


# ── Main entry ─────────────────────────────────────────────────────────────
def evaluate_v8_champion(
    surface: "FullDataSurface",
) -> Optional[StrategyDecision]:
    """Pre-gate hook. Always returns a concrete TRADE or SKIP.

    Exported name matches pre_gate_hook in v8_champion.yaml.
    """
    gates: list[dict] = []

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
        return _skip(
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
                return _skip(f"utc_hour_block: hour={hour}", gates)
            gates.append(
                _gate("utc_hour_block", True, f"hour={hour} allowed")
            )

    # ── 3. Regime ──────────────────────────────────────────────────────────
    v4_regime = surface.v4_regime
    tradeable = _tradeable_regimes()
    if v4_regime is None:
        gates.append(_gate("v4_regime", False, "regime=None (unknown)"))
        return _skip("v4_regime: regime unknown", gates)
    if v4_regime not in tradeable:
        gates.append(
            _gate(
                "v4_regime",
                False,
                f"regime={v4_regime} not in {sorted(tradeable)}",
            )
        )
        return _skip(f"v4_regime={v4_regime} not tradeable", gates)
    gates.append(_gate("v4_regime", True, f"regime={v4_regime} tradeable"))

    # ── 4. Source agreement (null-block) ───────────────────────────────────
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
        return _skip(
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
        return _skip("vpin: value unavailable", gates)
    if vpin < vmin or vpin > vmax:
        gates.append(
            _gate(
                "vpin",
                False,
                f"vpin={vpin:.3f} outside [{vmin:.2f},{vmax:.2f}]",
            )
        )
        return _skip(
            f"vpin={vpin:.3f} outside [{vmin:.2f},{vmax:.2f}]", gates
        )
    gates.append(
        _gate("vpin", True, f"vpin={vpin:.3f} in [{vmin:.2f},{vmax:.2f}]")
    )

    # ── 6. Ensemble bucket (HIGH conviction required) ──────────────────────
    probability_up = surface.poly_confidence
    if probability_up is None:
        gates.append(_gate("ensemble_bucket", False, "poly_confidence=None"))
        return _skip("ensemble_bucket: no probability_up", gates)

    direction = surface.poly_direction
    if direction is None:
        # Fall back: infer from probability_up.
        direction = "UP" if probability_up > 0.5 else "DOWN"

    p_lgb = getattr(surface, "probability_lgb", None)
    p_path1 = getattr(surface, "probability_classifier", None)
    bucket, is_agree_strong, is_pegged_path1 = _classify_bucket(
        p_lgb, p_path1, probability_up, direction
    )

    if not (is_agree_strong or is_pegged_path1):
        gates.append(
            _gate(
                "ensemble_bucket",
                False,
                f"bucket={bucket} (not HIGH conviction)",
            )
        )
        return _skip(
            f"ensemble_bucket: {bucket} not HIGH conviction",
            gates,
            direction=direction,
            bucket=bucket,
        )
    gates.append(
        _gate("ensemble_bucket", True, f"bucket={bucket} HIGH conviction")
    )

    # ── 7. Fill-band ───────────────────────────────────────────────────────
    # Use CLOB ask on the side we are buying (UP → clob_up_ask, DOWN →
    # clob_down_ask). Fall back to poly_max_entry_price if asks absent.
    if direction == "UP":
        fill_price = getattr(surface, "clob_up_ask", None)
    else:
        fill_price = getattr(surface, "clob_down_ask", None)
    if fill_price is None:
        fill_price = getattr(surface, "poly_max_entry_price", None)
    if fill_price is None:
        gates.append(_gate("fill_band", False, "no CLOB ask / poly_max_entry"))
        return _skip(
            "fill_band: no fill price available",
            gates,
            direction=direction,
            bucket=bucket,
        )

    fmin = _fill_band_min()
    fmax = _fill_band_max()
    if fill_price < fmin or fill_price > fmax:
        gates.append(
            _gate(
                "fill_band",
                False,
                f"fill={fill_price:.3f} outside [{fmin:.2f},{fmax:.2f}]",
            )
        )
        return _skip(
            f"fill_band: price={fill_price:.3f} outside "
            f"[{fmin:.2f},{fmax:.2f}]",
            gates,
            direction=direction,
            bucket=bucket,
        )
    gates.append(
        _gate(
            "fill_band",
            True,
            f"fill={fill_price:.3f} in [{fmin:.2f},{fmax:.2f}]",
        )
    )

    # ── 8. UP asymmetric ───────────────────────────────────────────────────
    if direction == "UP":
        up_min = _up_min_fill_price()
        if up_min > 0.0 and fill_price < up_min:
            gates.append(
                _gate(
                    "up_fill_floor",
                    False,
                    f"UP fill={fill_price:.3f} < {up_min:.2f}",
                )
            )
            return _skip(
                f"up_fill_below_min: fill={fill_price:.3f} < {up_min:.2f}",
                gates,
                direction=direction,
                bucket=bucket,
            )
        gates.append(
            _gate(
                "up_fill_floor",
                True,
                f"UP fill={fill_price:.3f} >= {up_min:.2f}",
            )
        )

        if _up_require_both_buckets() and not (
            is_agree_strong and is_pegged_path1
        ):
            gates.append(
                _gate(
                    "up_both_buckets",
                    False,
                    (
                        f"UP requires agree_strong AND pegged_path1; "
                        f"got agree_strong={is_agree_strong} "
                        f"pegged={is_pegged_path1}"
                    ),
                )
            )
            return _skip(
                "up_require_both_buckets: UP needs both buckets",
                gates,
                direction=direction,
                bucket=bucket,
            )
        gates.append(
            _gate(
                "up_both_buckets",
                True,
                f"UP agree_strong={is_agree_strong} pegged={is_pegged_path1}",
            )
        )

    # ── TRADE ──────────────────────────────────────────────────────────────
    distance = abs(probability_up - 0.5)
    conviction_label = "HIGH"
    confidence_score = min(distance * 2.0, 1.0)

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=conviction_label,
        confidence_score=confidence_score,
        entry_cap=_ENTRY_CAP,
        collateral_pct=None,  # custom clob_sizing schedule decides
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            f"v8_champion_{bucket}_{direction}_T{surface.eval_offset}_"
            f"f{fill_price:.2f}"
        ),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": distance,
            "poly_timing": getattr(surface, "poly_timing", None),
            "v4_regime": surface.v4_regime,
            "vpin": vpin,
            "vpin_regime": surface.regime,
            "conviction_bucket": bucket,
            "is_agree_strong": is_agree_strong,
            "is_pegged_path1": is_pegged_path1,
            "probability_lgb": p_lgb,
            "probability_classifier": p_path1,
            "probability_used": probability_up,
            "fill_price": fill_price,
            "chainlink_delta": surface.delta_chainlink,
            "tiingo_delta": surface.delta_tiingo,
        },
    )
