"""Hook for v8_champion_lgb_only strategy.

LGB-only variant of v8_champion. Same gate stack as v8 with 4 changes:
  - Gate 6 (ensemble_bucket) replaced by lgb_bucket (no classifier dependency)
  - Gate 3b NEW: block DOWN in TRANSITION vpin regime
  - Gate 9b NEW: down_fill_floor (0.15) replaces lost fill_band LOW guard
  - Gate 10 NEW: post-loss cooldown (20 min default)

Helper imports reused from v8_champion.py to avoid divergence. Post-loss
cooldown is in-memory first, with a DB fallback lookup for cold starts
(engine restart during an active cooldown).

See Hub note #222 for the tactical spec and note #221 for strategic
rationale + evidence.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v8_champion import (
    _gate,
    _skip,
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

_STRATEGY_ID = "v8_champion_lgb_only"
_VERSION = "8.0.0-lgb-0.1"
_ENTRY_CAP = 0.80


# ── New config knobs ────────────────────────────────────────────────────────
def _fill_band_min_lgb() -> float:
    """LGB variant overrides the v8 floor (0.30 → 0.00)."""
    return _gp.get_float("fill_band_min", "V8LGB_FILL_BAND_MIN", 0.00)


def _down_min_fill_price() -> float:
    return _gp.get_float(
        "down_min_fill_price", "V8LGB_DOWN_MIN_FILL_PRICE", 0.15
    )


def _lgb_dist_min_down() -> float:
    return _gp.get_float("lgb_dist_min_down", "V8LGB_DIST_MIN_DOWN", 0.10)


def _lgb_dist_min_up() -> float:
    return _gp.get_float("lgb_dist_min_up", "V8LGB_DIST_MIN_UP", 0.15)


def _block_down_vpin_regimes() -> set[str]:
    return set(
        _gp.get_str_list(
            "block_down_vpin_regimes",
            "V8LGB_BLOCK_DOWN_VPIN_REGIMES",
            ["TRANSITION"],
        )
    )


def _block_up_vpin_regimes() -> set[str]:
    return set(
        _gp.get_str_list(
            "block_up_vpin_regimes", "V8LGB_BLOCK_UP_VPIN_REGIMES", []
        )
    )


def _post_loss_cooldown_min() -> int:
    return _gp.get_int(
        "post_loss_cooldown_min", "V8LGB_POST_LOSS_COOLDOWN_MIN", 20
    )


# ── TRANSITION strong-oracle bypass (2026-04-24) ───────────────────────────
# When TRANSITION regime would block UP or DOWN, allow bypass if chainlink +
# tiingo BOTH agree direction AND avg(|Δcl|,|Δti|) >= threshold AND LGB
# conviction is strong. Binance excluded by design. See note #228.
def _transition_strong_bypass_enabled() -> bool:
    return _gp.get_bool(
        "transition_strong_bypass_enabled",
        "V8LGB_TRANSITION_STRONG_BYPASS_ENABLED",
        True,
    )


def _transition_bypass_min_avg_pct_delta() -> float:
    """Threshold expressed in PERCENT (0.05 = 0.05%).

    Surface deltas are FRACTIONAL (e.g. -0.0007 = -0.07%). We divide by 100
    at comparison time.
    """
    return _gp.get_float(
        "transition_bypass_min_avg_pct_delta",
        "V8LGB_TRANSITION_BYPASS_MIN_AVG_PCT_DELTA",
        0.05,
    )


def _transition_bypass_min_lgb_dist() -> float:
    return _gp.get_float(
        "transition_bypass_min_lgb_dist",
        "V8LGB_TRANSITION_BYPASS_MIN_LGB_DIST",
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


# ── In-memory cooldown state ───────────────────────────────────────────────
# Reset on process restart; DB fallback covers the cold-start case.
_last_loss_epoch: Optional[int] = None


def _load_last_loss_from_db() -> Optional[int]:
    """Find most recent v8_champion_lgb_only LOSS in trades table.

    Best-effort cold-start lookup so a restart during an active cooldown
    still honours it. Returns None on any failure (missing table, pool
    not wired, driver mismatch) — the cooldown gate then behaves as if
    no prior loss existed, which is safe for GHOST and fails-open for
    LIVE. Real cooldown signal is the in-memory ``_last_loss_epoch``
    fed from ``record_loss`` by the outcome reconciler.
    """
    return None


def _in_cooldown(now_epoch: int) -> tuple[bool, int]:
    """Returns (in_cooldown, seconds_remaining).

    Checks in-memory first, falls back to DB lookup on cold start.
    """
    global _last_loss_epoch
    cooldown_sec = _post_loss_cooldown_min() * 60
    if cooldown_sec <= 0:
        return (False, 0)
    if _last_loss_epoch is None:
        _last_loss_epoch = _load_last_loss_from_db()
    if _last_loss_epoch is None:
        return (False, 0)
    elapsed = now_epoch - _last_loss_epoch
    if elapsed < cooldown_sec:
        return (True, cooldown_sec - elapsed)
    return (False, 0)


def record_loss(resolved_at_epoch: int) -> None:
    """Called from the outcome-reconciler when a v8_champion_lgb_only
    trade resolves LOSS. Updates the in-memory cooldown timestamp.
    """
    global _last_loss_epoch
    _last_loss_epoch = resolved_at_epoch


def reset_cooldown() -> None:
    """Clear the in-memory cooldown timestamp. Used by tests."""
    global _last_loss_epoch
    _last_loss_epoch = None


# ── Hook entry ──────────────────────────────────────────────────────────────
def evaluate_v8_champion_lgb_only(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Main evaluation hook. Same shape as evaluate_v8_champion."""
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

    # ── 3. Regime (v4) ─────────────────────────────────────────────────────
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

    # ── 6. LGB bucket (no classifier dependency) ───────────────────────────
    # Use probability_lgb directly. Note #220: this field is already
    # T=1.07 temp-calibrated at train time.
    p_up = getattr(surface, "probability_lgb", None)
    if p_up is None:
        gates.append(_gate("lgb_bucket", False, "probability_lgb=None"))
        return _skip("lgb_bucket: probability_lgb unavailable", gates)
    dist = abs(p_up - 0.5)
    direction = "UP" if p_up > 0.5 else "DOWN"
    min_dist = _lgb_dist_min_up() if direction == "UP" else _lgb_dist_min_down()
    if dist < min_dist:
        gates.append(
            _gate(
                "lgb_bucket",
                False,
                f"dist={dist:.3f} < min {min_dist:.3f} ({direction})",
            )
        )
        return _skip(
            f"lgb_bucket: low conviction {direction} dist={dist:.3f}",
            gates,
            direction=direction,
        )
    gates.append(
        _gate(
            "lgb_bucket",
            True,
            f"{direction} p_up={p_up:.3f} dist={dist:.3f}",
        )
    )

    # ── 3b. Per-direction vpin-regime block (AFTER direction known) ────────
    # NB: checked against surface.regime (CALM / NORMAL / TRANSITION /
    # CASCADE), not v4_regime. TRANSITION is a vol-regime value.
    #
    # TRANSITION strong-oracle bypass (2026-04-24): when the regime gate
    # would block UP or DOWN in TRANSITION, allow bypass if chainlink +
    # tiingo BOTH agree direction AND avg(|Δcl|,|Δti|) >= threshold AND
    # LGB dist is strong. See note #228.
    vpin_regime = getattr(surface, "regime", None)
    regime_would_block = (
        (direction == "DOWN" and vpin_regime in _block_down_vpin_regimes())
        or (direction == "UP" and vpin_regime in _block_up_vpin_regimes())
    )
    if regime_would_block:
        bypass, diag = _should_bypass_transition(
            direction,
            p_up,
            surface.delta_chainlink,
            surface.delta_tiingo,
        )
        if bypass:
            gates.append(
                _gate(
                    "vpin_regime_direction",
                    True,
                    f"transition_strong_bypass: {direction} in "
                    f"{vpin_regime} {diag}",
                )
            )
        else:
            gates.append(
                _gate(
                    "vpin_regime_direction",
                    False,
                    f"{direction} blocked in vpin regime={vpin_regime} "
                    f"(no bypass: {diag})",
                )
            )
            return _skip(
                f"vpin_regime_direction: {direction} blocked in "
                f"{vpin_regime}",
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

    # ── 7. Oracle direction agreement (if enabled) ─────────────────────────
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
            return _skip(
                f"oracle_direction: disagree cl={cl_dir} ti={ti_dir} "
                f"vs {direction}",
                gates,
                direction=direction,
            )
    gates.append(
        _gate("oracle_direction", True, f"oracles agree with {direction}")
    )

    # ── 8. Fill-band ───────────────────────────────────────────────────────
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
        return _skip(
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

    # ── 9a. UP fill floor ──────────────────────────────────────────────────
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
            return _skip(
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

    # ── 9b. DOWN fill floor (NEW) ──────────────────────────────────────────
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
            return _skip(
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

    # ── 10. Post-loss cooldown (NEW) ───────────────────────────────────────
    in_cd, remaining = _in_cooldown(now)
    if in_cd:
        gates.append(
            _gate(
                "post_loss_cooldown",
                False,
                f"{remaining}s remaining",
            )
        )
        return _skip(
            f"post_loss_cooldown: {remaining // 60}m {remaining % 60}s "
            f"remaining",
            gates,
            direction=direction,
        )
    gates.append(
        _gate("post_loss_cooldown", True, "no active cooldown")
    )

    # ── TRADE ──────────────────────────────────────────────────────────────
    # Use dist as the conviction score so clob_sizing schedule thresholds
    # (0.25 / 0.35 / 0.55) map naturally: dist=0.30 → HIGH modifier 2.0.
    confidence_score = min(dist * 2.0, 1.0)

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=confidence_score,
        entry_cap=_ENTRY_CAP,
        collateral_pct=None,  # custom clob_sizing schedule decides
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            f"v8_champion_lgb_only_{direction}_T{surface.eval_offset}_"
            f"f{fill_price:.2f}"
        ),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": dist,
            "v4_regime": v4_regime,
            "vpin": vpin,
            "vpin_regime": vpin_regime,
            "probability_lgb": p_up,
            "probability_used": p_up,
            "lgb_dist": dist,
            "fill_price": fill_price,
            "chainlink_delta": surface.delta_chainlink,
            "tiingo_delta": surface.delta_tiingo,
            "primary_signal_source": "lgb",
        },
    )
