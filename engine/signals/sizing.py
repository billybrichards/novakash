"""
v10.3 Edge-Weighted Kelly Sizing — from decision surface spec (Section 7).

Replaces flat BET_FRACTION with composite Kelly sizing that accounts for:
  - Signal strength (ELM probability → edge → half-Kelly)
  - Time quality (T-60 = 1.0, T-180 = 0.7 — accuracy degrades with distance)
  - Direction quality (UP = 1.0, DOWN = 0.85 — 9.3pp accuracy gap)
  - CoinGlass confirmation (2+ confirms = 1.15x — derivatives agree with model)
  - Regime quality (NORMAL/LOW_VOL = 1.0, others = 0.85 — volatile = cautious)

Feature flag: V10_KELLY_ENABLED (default false). When disabled, falls back to
flat bankroll * BET_FRACTION from runtime_config.

Usage:
    from signals.sizing import compute_position_size

    stake = compute_position_size(
        p_up=0.78, direction="UP", seconds_to_close=60,
        regime="NORMAL", cg_confirms=3, bankroll=130.0,
    )
    # → $37.70 (capped to ABSOLUTE_MAX_BET in caller)
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)


def compute_position_size(
    p_up: float,
    direction: str,
    seconds_to_close: int,
    regime: str,
    cg_confirms: int,
    bankroll: float,
    kelly_shrink: float | None = None,
    cap_ceiling: float | None = None,
) -> float:
    """Compute edge-weighted Kelly position size.

    Args:
        p_up: ELM model P(UP) in [0, 1].
        direction: "UP" or "DOWN" — agreed direction from source agreement gate.
        seconds_to_close: Seconds until window close (60-180 typical range).
        regime: VPIN regime string (NORMAL, CASCADE, TRANSITION, etc.).
        cg_confirms: Number of CoinGlass confirmation signals (0-3).
        bankroll: Current available bankroll in USD.
        kelly_shrink: Half-Kelly shrink factor (default from env V10_KELLY_SHRINK).
        cap_ceiling: Maximum Kelly fraction (default from env V10_DUNE_CAP_CEILING).

    Returns:
        Position size in USD. Caller should still apply ABSOLUTE_MAX_BET cap.
    """
    if kelly_shrink is None:
        kelly_shrink = float(os.environ.get("V10_KELLY_SHRINK", "0.50"))
    if cap_ceiling is None:
        cap_ceiling = float(os.environ.get("V10_DUNE_CAP_CEILING", "0.68"))

    cg_confirm_min = int(os.environ.get("V10_CG_CONFIRM_MIN", "2"))

    # P in the agreed direction
    p_dir = p_up if direction == "UP" else (1.0 - p_up)

    # Edge = how much better than coin flip (range [-1, 1])
    edge = 2 * p_dir - 1
    base_kelly = max(0.0, kelly_shrink * edge)

    # Time quality: linearly decays from 1.0 at T-60 to 0.7 at T-180
    # Rationale: ELM accuracy drops from 76.5% (T-60) to 63.2% (T-180)
    time_mult = max(0.7, 1.0 - (max(0, seconds_to_close - 60) / 120) * 0.3)

    # Direction quality: DOWN predictions are 9.3pp less accurate (N=865)
    dir_mult = 1.0 if direction == "UP" else 0.85

    # CoinGlass confirmation: +20% when 2+ derivatives signals agree (was 15%, strengthened)
    cg_mult = 1.20 if cg_confirms >= cg_confirm_min else 1.0

    # Regime quality: NORMAL/LOW_VOL are stable; others get 15% reduction
    regime_mult = 1.0 if regime in ("NORMAL", "LOW_VOL") else 0.85

    # Composite
    sized = base_kelly * time_mult * dir_mult * cg_mult * regime_mult
    sized = min(sized, cap_ceiling)
    position = sized * bankroll

    log.info("kelly.computed",
        p_dir=f"{p_dir:.3f}", edge=f"{edge:.3f}",
        base_kelly=f"{base_kelly:.3f}",
        time_mult=f"{time_mult:.2f}", dir_mult=f"{dir_mult:.2f}",
        cg_mult=f"{cg_mult:.2f}", regime_mult=f"{regime_mult:.2f}",
        sized=f"{sized:.3f}", position=f"${position:.2f}",
        direction=direction, regime=regime, offset=seconds_to_close,
        cg_confirms=cg_confirms)

    return position
