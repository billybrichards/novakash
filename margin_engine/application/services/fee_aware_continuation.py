"""
Service: Fee-aware continuation decision logic.

This module implements the fee-aware continuation decision system that:
1. Uses fee-adjusted PnL for continuation decisions
2. Implements partial take-profit logic
3. Calculates hold time extensions based on signal strength
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import TradeSide, V4Snapshot, TimescalePayload


class ContinuationDecision(Enum):
    """Decision types for position continuation."""

    CONTINUE = "CONTINUE"  # Hold position (signals strong, profitable)
    CONTINUE_EXTENDED = "CONTINUE_EXTENDED"  # Hold with extended time
    CLOSE_PARTIAL = "CLOSE_PARTIAL"  # Close 50% (profitable but weakening)
    CLOSE_ALL = "CLOSE_ALL"  # Close position (signals flipped or unprofitable)


@dataclass
class FeeAwareContinuationResult:
    """Result of fee-aware continuation decision."""

    decision: ContinuationDecision
    reason: str
    hold_extension_mult: float = 1.0  # Multiplier for hold time
    partial_close_pct: Optional[float] = None  # If CLOSE_PARTIAL, what % to close
    net_pnl: float = 0.0  # Fee-adjusted PnL at decision time
    timescale_aligned: int = 0  # Number of aligned timescales (0-4)


def calculate_fee_adjusted_pnl(
    position: Position,
    mark_price: float,
    fee_rate: float = 0.001,
) -> float:
    """
    Calculate fee-adjusted unrealised PnL.

    Args:
        position: The open position
        mark_price: Current mark price (bid for LONG, ask for SHORT)
        fee_rate: Fee rate per side (default 0.1%)

    Returns:
        Net unrealised PnL after accounting for entry fee,
        estimated exit fee, and borrow interest.
    """
    return position.unrealised_pnl_net(mark_price)


def get_tp_progress(
    position: Position,
    mark_price: float,
) -> Optional[float]:
    """
    Calculate progress towards take-profit.

    Returns:
        Float in [0, 1] representing progress to TP, or None if no TP set.
    """
    if not position.take_profit or not position.entry_price:
        return None

    net_pnl = calculate_fee_adjusted_pnl(position, mark_price)
    tp_distance = abs(position.take_profit.price - position.entry_price.value)

    if tp_distance == 0:
        return None

    # Calculate PnL towards TP based on side
    if position.side == TradeSide.LONG:
        current_pnl = mark_price - position.entry_price.value
    else:
        current_pnl = position.entry_price.value - mark_price

    progress = current_pnl / tp_distance if tp_distance > 0 else 0
    return max(0.0, min(1.0, progress))


def should_take_partial_profit(
    position: Position,
    mark_price: float,
    v4: Optional[V4Snapshot] = None,
    threshold: float = 0.5,  # 50% of TP
    partial_size: float = 0.5,  # Close 50%
    conviction_min: float = 0.15,  # Weak signal threshold
) -> Optional[float]:
    """
    Determine if we should take partial profit.

    Triggers:
    - At 75% of TP → close 25% (lock in gains)
    - At 50% of TP + signals weakening → close 50%

    Args:
        position: The open position
        mark_price: Current mark price
        v4: Optional v4 snapshot for signal analysis
        threshold: Progress threshold to consider partial (default 0.5 = 50%)
        partial_size: Size of partial close (default 0.5 = 50%)
        conviction_min: Minimum |p-0.5| to consider signal strong

    Returns:
        Float representing % to close (e.g., 0.5 = close 50%), or None
    """
    progress = get_tp_progress(position, mark_price)

    if progress is None:
        return None

    # At 75% of TP → close 25% (lock in gains)
    if progress >= 0.75:
        return 0.25

    # At 50% of TP + signals weakening → close 50%
    if progress >= threshold:
        if v4 is not None:
            ts = v4.timescales.get(position.entry_timescale)
            if ts and ts.probability_up is not None:
                conviction = abs(ts.probability_up - 0.5)
                if conviction < conviction_min:  # Weak signal
                    return partial_size

    return None


def calculate_signal_strength(
    ts: TimescalePayload,
) -> float:
    """
    Calculate signal strength score for a timescale.

    Factors:
    - Probability conviction (|p_up - 0.5|)
    - Regime quality

    Returns:
        Float in [0, 2.0] representing signal strength
    """
    if ts.probability_up is None:
        return 0.0

    # Base on probability conviction
    conviction = abs(ts.probability_up - 0.5)
    base_score = 1.0 + (conviction * 2)  # 0.5 to 2.0 range

    # Regime quality
    if ts.regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base_score *= 1.2
    elif ts.regime == "CHOPPY":
        base_score *= 0.7

    return base_score


def calculate_hold_extension(
    v4: V4Snapshot,
    position: Position,
    max_extension: float = 2.0,
    min_conviction: float = 0.10,
    regime_bonus: bool = True,
) -> float:
    """
    Calculate hold time extension based on signal strength.

    Args:
        v4: V4 snapshot with timescale data
        position: The open position
        max_extension: Maximum hold extension multiplier (default 2.0)
        min_conviction: Minimum |p-0.5| to consider signal valid
        regime_bonus: Whether to apply regime-based bonus

    Returns:
        Hold time multiplier (0.5x to 2.0x)
    """
    ts = v4.timescales.get(position.entry_timescale)
    if not ts or ts.probability_up is None:
        return 1.0

    # Check conviction
    conviction = abs(ts.probability_up - 0.5)
    if conviction < min_conviction:
        return 0.5  # Weak signal, reduce hold time

    # Base on probability conviction
    base_mult = 1.0 + (conviction * 2)  # 0.5 to 2.0 range

    # Alignment bonus
    alignment = check_continuation_alignment(v4, position)
    if alignment.aligned_count == 4:
        base_mult *= 1.3
    elif alignment.aligned_count == 3:
        base_mult *= 1.15

    # Regime quality
    if regime_bonus:
        if ts.regime in ("TRENDING_UP", "TRENDING_DOWN"):
            base_mult *= 1.2
        elif ts.regime == "CHOPPY":
            base_mult *= 0.7

    # Cap at max_extension
    return min(max_extension, max(0.5, base_mult))


def check_continuation_alignment(
    v4: V4Snapshot,
    position: Position,
) -> "AlignmentResult":
    """
    Check if timescales support continuation.

    Unlike entry (3/4 required), continuation is more lenient:
    - 4/4 aligned → extend hold 2.0x
    - 3/4 aligned → extend hold 1.5x
    - 2/4 aligned → normal hold
    - 0-1/4 aligned → exit (PROBABILITY_REVERSAL)

    Args:
        v4: V4 snapshot with timescale data
        position: The open position

    Returns:
        AlignmentResult with aligned count and hold multiplier
    """
    primary_direction = position.side

    aligned_count = 0
    timescale_names = ("5m", "15m", "1h", "4h")

    for ts_name in timescale_names:
        ts_data = v4.timescales.get(ts_name)
        if ts_data is None or ts_data.probability_up is None:
            continue

        ts_direction = (
            TradeSide.LONG if ts_data.probability_up >= 0.5 else TradeSide.SHORT
        )
        if ts_direction == primary_direction:
            aligned_count += 1

    if aligned_count <= 1:
        return AlignmentResult(
            aligned_count=aligned_count, should_continue=False, hold_mult=0.5
        )

    if aligned_count == 4:
        return AlignmentResult(aligned_count=4, should_continue=True, hold_mult=2.0)
    elif aligned_count == 3:
        return AlignmentResult(aligned_count=3, should_continue=True, hold_mult=1.5)
    else:  # 2/4
        return AlignmentResult(aligned_count=2, should_continue=True, hold_mult=1.0)


@dataclass
class AlignmentResult:
    """Result of timescale alignment check."""

    aligned_count: int
    should_continue: bool
    hold_mult: float


def fee_aware_continuation_decision(
    position: Position,
    mark_price: float,
    v4: Optional[V4Snapshot] = None,
    fee_aware_enabled: bool = True,
    alignment_enabled: bool = True,
    partial_tp_threshold: float = 0.5,
    partial_tp_size: float = 0.5,
    max_extension: float = 2.0,
    min_conviction: float = 0.10,
    regime_bonus: bool = True,
) -> FeeAwareContinuationResult:
    """
    Make continuation decision considering fee-adjusted PnL and multi-timescale alignment.

    Decision precedence:
    1. Check partial take-profit (at 75% TP or 50% TP with weak signals)
    2. Check timescale alignment (0-1/4 → exit)
    3. Check fee-adjusted PnL and signal strength
    4. Calculate hold extension

    Args:
        position: The open position
        mark_price: Current mark price
        v4: V4 snapshot with timescale data
        fee_aware_enabled: Whether to use fee-aware logic
        alignment_enabled: Whether to use multi-timescale alignment
        partial_tp_threshold: Progress threshold for partial TP
        partial_tp_size: Size of partial close
        max_extension: Maximum hold extension multiplier
        min_conviction: Minimum |p-0.5| to consider signal valid
        regime_bonus: Whether to apply regime-based bonus

    Returns:
        FeeAwareContinuationResult with decision and metadata
    """
    net_pnl = calculate_fee_adjusted_pnl(position, mark_price)

    # Scenario: Partial take-profit
    if v4 is not None:
        partial_close = should_take_partial_profit(
            position,
            mark_price,
            v4,
            threshold=partial_tp_threshold,
            partial_size=partial_tp_size,
        )
        if partial_close is not None:
            return FeeAwareContinuationResult(
                decision=ContinuationDecision.CLOSE_PARTIAL,
                reason="PARTIAL_TAKE_PROFIT",
                partial_close_pct=partial_close,
                net_pnl=net_pnl,
            )

    # Check timescale alignment
    if v4 is not None and alignment_enabled:
        alignment = check_continuation_alignment(v4, position)
        if not alignment.should_continue:
            return FeeAwareContinuationResult(
                decision=ContinuationDecision.CLOSE_ALL,
                reason="TIMESCALE_MISALIGNMENT",
                net_pnl=net_pnl,
                timescale_aligned=alignment.aligned_count,
                hold_extension_mult=0.5,
            )

    # Scenario 1: Deep in profit + strong signals → CONTINUE with extended hold
    if v4 is not None and fee_aware_enabled:
        ts = v4.timescales.get(position.entry_timescale)
        if ts and ts.probability_up is not None:
            conviction = abs(ts.probability_up - 0.5)
            signals_strong = conviction >= 0.15  # p_up > 0.65 or < 0.35

            # Get TP progress for deep profit check
            progress = get_tp_progress(position, mark_price)
            if progress is not None and progress > 0.7 and signals_strong:
                hold_ext = calculate_hold_extension(
                    v4, position, max_extension, min_conviction, regime_bonus
                )
                return FeeAwareContinuationResult(
                    decision=ContinuationDecision.CONTINUE_EXTENDED,
                    reason="DEEP_PROFIT_STRONG_SIGNAL",
                    hold_extension_mult=hold_ext,
                    net_pnl=net_pnl,
                    timescale_aligned=alignment.aligned_count if v4 else 0,
                )

    # Scenario 2: Profitable after fees + signals weakening → CLOSE_PARTIAL
    if v4 is not None and net_pnl > 0:
        ts = v4.timescales.get(position.entry_timescale)
        if ts and ts.probability_up is not None:
            conviction = abs(ts.probability_up - 0.5)
            if conviction < min_conviction:  # Signals weakening
                return FeeAwareContinuationResult(
                    decision=ContinuationDecision.CLOSE_PARTIAL,
                    reason="PROFITABLE_WEAK_SIGNALS",
                    partial_close_pct=partial_tp_size,
                    net_pnl=net_pnl,
                )

    # Scenario 3: Profitable after fees + signals strong but near TP → HOLD
    if v4 is not None and net_pnl > 0:
        ts = v4.timescales.get(position.entry_timescale)
        if ts and ts.probability_up is not None:
            conviction = abs(ts.probability_up - 0.5)
            if conviction >= min_conviction:
                hold_ext = calculate_hold_extension(
                    v4, position, max_extension, min_conviction, regime_bonus
                )
                return FeeAwareContinuationResult(
                    decision=ContinuationDecision.CONTINUE,
                    reason="PROFITABLE_STRONG_SIGNAL",
                    hold_extension_mult=hold_ext,
                    net_pnl=net_pnl,
                    timescale_aligned=alignment.aligned_count if v4 else 0,
                )

    # Scenario 4: Unprofitable + signals flipped → CLOSE_ALL
    if v4 is not None and net_pnl < 0:
        ts = v4.timescales.get(position.entry_timescale)
        if ts:
            ts_direction = (
                TradeSide.LONG if ts.probability_up >= 0.5 else TradeSide.SHORT
            )
            if ts_direction != position.side:
                return FeeAwareContinuationResult(
                    decision=ContinuationDecision.CLOSE_ALL,
                    reason="PROBABILITY_REVERSAL",
                    net_pnl=net_pnl,
                )

    # Scenario 5: Breakeven + mixed signals → CONTINUE (normal)
    hold_ext = 1.0
    if v4 is not None:
        hold_ext = calculate_hold_extension(
            v4, position, max_extension, min_conviction, regime_bonus
        )

    return FeeAwareContinuationResult(
        decision=ContinuationDecision.CONTINUE,
        reason="BREAKEVEN_CONTINUATION",
        hold_extension_mult=hold_ext,
        net_pnl=net_pnl,
        timescale_aligned=alignment.aligned_count if v4 else 0,
    )
