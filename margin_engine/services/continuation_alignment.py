"""
Service: Multi-timescale continuation alignment checking.

This module implements the multi-timescale alignment logic for position
continuation decisions. It checks if multiple timescales (5m, 15m, 1h, 4h)
support continuing the position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    TradeSide,
    V4Snapshot,
    TimescalePayload,
)


@dataclass
class TimescaleAlignment:
    """Alignment status for a single timescale."""

    timescale: str
    aligned: bool
    probability_up: Optional[float]
    direction: Optional[TradeSide]
    conviction: float = 0.0


@dataclass
class AlignmentResult:
    """Result of multi-timescale alignment check."""

    aligned_count: int
    total_timescales: int
    should_continue: bool
    hold_mult: float
    reason: str
    breakdown: List[TimescaleAlignment] = None

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = []


def get_timescale_agreement(
    v4: V4Snapshot,
    position: Position,
    timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
) -> List[TimescaleAlignment]:
    """
    Get per-timescale agreement breakdown.

    Args:
        v4: V4 snapshot with timescale data
        position: The open position
        timescales: Timescales to check (default: 5m, 15m, 1h, 4h)

    Returns:
        List of TimescaleAlignment for each timescale
    """
    primary_direction = position.side
    breakdown = []

    for ts_name in timescales:
        ts_data = v4.timescales.get(ts_name)

        if ts_data is None:
            breakdown.append(
                TimescaleAlignment(
                    timescale=ts_name,
                    aligned=False,
                    probability_up=None,
                    direction=None,
                )
            )
            continue

        if ts_data.probability_up is None:
            breakdown.append(
                TimescaleAlignment(
                    timescale=ts_name,
                    aligned=False,
                    probability_up=None,
                    direction=None,
                )
            )
            continue

        ts_direction = (
            TradeSide.LONG if ts_data.probability_up >= 0.5 else TradeSide.SHORT
        )
        aligned = ts_direction == primary_direction
        conviction = abs(ts_data.probability_up - 0.5)

        breakdown.append(
            TimescaleAlignment(
                timescale=ts_name,
                aligned=aligned,
                probability_up=ts_data.probability_up,
                direction=ts_direction,
                conviction=conviction,
            )
        )

    return breakdown


def check_continuation_alignment(
    v4: V4Snapshot,
    position: Position,
    timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
    min_timescales: int = 2,
) -> AlignmentResult:
    """
    Check if timescales support continuation.

    Unlike entry (3/4 required), continuation is more lenient:
    - 4/4 aligned → extend hold 2.0x
    - 3/4 aligned → extend hold 1.5x
    - 2/4 aligned → normal hold (1.0x)
    - 0-1/4 aligned → exit (PROBABILITY_REVERSAL)

    Args:
        v4: V4 snapshot with timescale data
        position: The open position
        timescales: Timescales to check (default: 5m, 15m, 1h, 4h)
        min_timescales: Minimum aligned timescales to continue (default 2)

    Returns:
        AlignmentResult with alignment count, continuation decision, and hold multiplier
    """
    breakdown = get_timescale_agreement(v4, position, timescales)

    # Count aligned timescales
    aligned_count = sum(1 for t in breakdown if t.aligned)
    total_timescales = len([t for t in breakdown if t.probability_up is not None])

    # Handle edge cases
    if total_timescales == 0:
        return AlignmentResult(
            aligned_count=0,
            total_timescales=0,
            should_continue=False,
            hold_mult=0.5,
            reason="NO_TIMESCALE_DATA",
            breakdown=breakdown,
        )

    # Decision logic
    if aligned_count <= 1:
        return AlignmentResult(
            aligned_count=aligned_count,
            total_timescales=total_timescales,
            should_continue=False,
            hold_mult=0.5,
            reason="TIMESCALE_MISALIGNMENT",
            breakdown=breakdown,
        )

    if aligned_count == 4:
        return AlignmentResult(
            aligned_count=aligned_count,
            total_timescales=total_timescales,
            should_continue=True,
            hold_mult=2.0,
            reason="FULL_ALIGNMENT",
            breakdown=breakdown,
        )
    elif aligned_count == 3:
        return AlignmentResult(
            aligned_count=aligned_count,
            total_timescales=total_timescales,
            should_continue=True,
            hold_mult=1.5,
            reason="STRONG_ALIGNMENT",
            breakdown=breakdown,
        )
    elif aligned_count >= min_timescales:
        return AlignmentResult(
            aligned_count=aligned_count,
            total_timescales=total_timescales,
            should_continue=True,
            hold_mult=1.0,
            reason="MINIMAL_ALIGNMENT",
            breakdown=breakdown,
        )
    else:
        return AlignmentResult(
            aligned_count=aligned_count,
            total_timescales=total_timescales,
            should_continue=False,
            hold_mult=0.5,
            reason="BELOW_MINIMUM_ALIGNMENT",
            breakdown=breakdown,
        )


def calculate_alignment_score(
    v4: V4Snapshot,
    position: Position,
    timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
) -> float:
    """
    Calculate a continuous alignment score for ranking purposes.

    Returns:
        Float in [0, 1] where 1.0 = perfect alignment across all timescales
    """
    breakdown = get_timescale_agreement(v4, position, timescales)

    if not breakdown:
        return 0.0

    total = 0.0
    count = 0

    for t in breakdown:
        if t.probability_up is None:
            continue

        # Base score from conviction
        conviction = abs(t.probability_up - 0.5) * 2  # Normalize to [0, 1]

        # Bonus if aligned
        if t.aligned:
            total += 0.5 + conviction
        else:
            total += 0.5 - conviction

        count += 1

    if count == 0:
        return 0.0

    return max(0.0, min(1.0, total / count))


def get_primary_timescale_signal(
    v4: V4Snapshot,
    position: Position,
) -> Optional[TimescalePayload]:
    """
    Get the signal for the position's entry timescale.

    Args:
        v4: V4 snapshot
        position: The open position

    Returns:
        TimescalePayload for entry timescale, or None if not available
    """
    return v4.timescales.get(position.entry_timescale)


def check_signal_strength_at_timescale(
    ts: TimescalePayload,
    min_conviction: float = 0.10,
) -> bool:
    """
    Check if a timescale has sufficient signal strength.

    Args:
        ts: Timescale payload
        min_conviction: Minimum |p-0.5| required

    Returns:
        True if signal is strong enough
    """
    if ts.probability_up is None:
        return False

    conviction = abs(ts.probability_up - 0.5)
    return conviction >= min_conviction


def get_regime_quality(ts: TimescalePayload) -> float:
    """
    Get regime quality score for a timescale.

    Returns:
        Float multiplier based on regime:
        - TRENDING_UP/TRENDING_DOWN: 1.2
        - MEAN_REVERTING: 1.0
        - CHOPPY: 0.7
        - NO_EDGE: 0.5
        - Unknown: 1.0
    """
    regime_scores = {
        "TRENDING_UP": 1.2,
        "TRENDING_DOWN": 1.2,
        "MEAN_REVERTING": 1.0,
        "CHOPPY": 0.7,
        "NO_EDGE": 0.5,
    }

    if ts.regime is None:
        return 1.0

    return regime_scores.get(ts.regime, 1.0)
