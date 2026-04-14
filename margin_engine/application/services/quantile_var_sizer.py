"""
Quantile-VaR Position Sizer — risk-parity sizing using TimesFM quantiles.

This service calculates Value-at-Risk from TimesFM quantile forecasts and
uses inverse-VaR sizing to achieve constant $ risk per trade regardless of
volatility.

Background:
- Current sizing: Fixed Kelly fraction (2% of equity)
- Problem: Same $ risk on low-vol vs high-vol trades
- Solution: Inverse-VaR sizing (constant $ risk per trade)
- Data: V4 provides p10, p25, p50, p75, p90 quantiles for each timescale

Logic:
- Low VaR (low vol) → Larger position (same $ risk)
- High VaR (high vol) → Smaller position (same $ risk)

Example:
    from margin_engine.domain.value_objects import V4Snapshot
    from margin_engine.application.services.quantile_var_sizer import (
        calculate_var,
        calculate_position_size_mult,
        VaRResult
    )

    # Calculate VaR from V4 snapshot
    var_result = calculate_var(v4_snapshot, timescale="15m")
    if var_result:
        # Calculate size multiplier (default target risk 0.5%)
        size_mult = calculate_position_size_mult(
            var_result,
            base_size_usd=10.0,
            target_risk_pct=0.005
        )
        # size_mult will be in [0.5, 2.0] range
        final_size = 10.0 * size_mult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from margin_engine.domain.value_objects import V4Snapshot, TimescalePayload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VaRResult:
    """Result of VaR calculation from TimesFM quantiles.

    Attributes:
        downside_var_pct: P10 downside as % of median price
        upside_var_pct: P90 upside as % of median price
        expected_move_pct: Full P90-P10 range as % of median
        confidence_interval_pct: Same as expected_move_pct (90% CI)
        var_bps: Downside VaR in basis points
        position_size_mult: Size multiplier based on inverse-VaR sizing
    """

    downside_var_pct: float
    upside_var_pct: float
    expected_move_pct: float
    confidence_interval_pct: float
    position_size_mult: float
    var_bps: int


def calculate_var(
    v4: V4Snapshot,
    timescale: str = "15m",
    target_risk_pct: float = 0.005,
    min_mult: float = 0.5,
    max_mult: float = 2.0,
) -> Optional[VaRResult]:
    """
    Calculate VaR from TimesFM quantiles and compute position size multiplier.

    Args:
        v4: V4 snapshot with quantiles
        timescale: Which timescale to use (default "15m")
        target_risk_pct: Target risk as % of equity (default 0.5%)
        min_mult: Minimum size multiplier (default 0.5)
        max_mult: Maximum size multiplier (default 2.0)

    Returns:
        VaRResult with all metrics including position size multiplier,
        or None if data is missing.

    Example scenarios:
        Low volatility:
            p10=72500, p50=73000, p90=73500
            downside_var_pct = (73000 - 72500) / 73000 = 0.68%
            size_mult = 0.5 / 0.68 = 0.74x

        High volatility:
            p10=71000, p50=73000, p90=76000
            downside_var_pct = (73000 - 71000) / 73000 = 2.74%
            size_mult = 0.5 / 2.74 = 0.18x → capped at 0.5x

        Very low volatility:
            p10=72800, p50=73000, p90=73200
            downside_var_pct = (73000 - 72800) / 73000 = 0.27%
            size_mult = 0.5 / 0.27 = 1.85x
    """
    ts = v4.timescales.get(timescale)
    if not ts:
        logger.debug(f"Timescale '{timescale}' not found in V4 snapshot")
        return None

    return calculate_var_from_payload(
        ts,
        target_risk_pct=target_risk_pct,
        min_mult=min_mult,
        max_mult=max_mult,
    )


def calculate_var_from_payload(
    payload: TimescalePayload,
    target_risk_pct: float = 0.005,
    min_mult: float = 0.5,
    max_mult: float = 2.0,
) -> Optional[VaRResult]:
    """
    Calculate VaR from a timescale payload.

    Args:
        payload: Timescale payload with quantiles_at_close
        target_risk_pct: Target risk as % of equity (default 0.5%)
        min_mult: Minimum size multiplier (default 0.5)
        max_mult: Maximum size multiplier (default 2.0)

    Returns:
        VaRResult or None if quantiles are missing.
    """
    q = payload.quantiles_at_close
    if not q:
        logger.debug("Quantiles not available in timescale payload")
        return None

    p10 = q.p10
    p25 = q.p25
    p50 = q.p50
    p75 = q.p75
    p90 = q.p90

    # Need at least p10 and p50 for downside VaR
    if p10 is None or p50 is None:
        logger.debug("Missing p10 or p50 quantiles")
        return None

    # p90 is optional (for upside VaR)
    if p90 is None:
        p90 = p50  # Assume symmetric if missing

    # Calculate VaR as percentage of median
    downside_var_pct = (p50 - p10) / p50
    upside_var_pct = (p90 - p50) / p50
    expected_move_pct = (p90 - p10) / p50

    # Convert to basis points
    var_bps = int(downside_var_pct * 10000)

    # Calculate position size multiplier using inverse-VaR sizing
    # If VaR is 1% and target risk is 0.5%, size = 0.5 / 1.0 = 0.5x
    # If VaR is 0.25% and target risk is 0.5%, size = 0.5 / 0.25 = 2.0x
    position_size_mult = _calculate_size_mult(
        downside_var_pct,
        target_risk_pct=target_risk_pct,
        min_mult=min_mult,
        max_mult=max_mult,
    )

    return VaRResult(
        downside_var_pct=downside_var_pct,
        upside_var_pct=upside_var_pct,
        expected_move_pct=expected_move_pct,
        confidence_interval_pct=expected_move_pct,
        position_size_mult=position_size_mult,
        var_bps=var_bps,
    )


def _calculate_size_mult(
    downside_var_pct: float,
    target_risk_pct: float = 0.005,
    min_mult: float = 0.5,
    max_mult: float = 2.0,
) -> float:
    """
    Calculate position size multiplier using inverse-VaR sizing.

    Target: Constant $ risk per trade regardless of volatility.

    Args:
        downside_var_pct: Downside VaR as percentage (e.g., 0.01 for 1%)
        target_risk_pct: Target risk as % of equity (default 0.5%)
        min_mult: Minimum size multiplier (default 0.5)
        max_mult: Maximum size multiplier (default 2.0)

    Returns:
        Size multiplier clamped to [min_mult, max_mult] range.
    """
    if downside_var_pct == 0:
        # Default to 1.0x if no VaR data
        return 1.0

    # Inverse-VaR sizing
    size_mult = target_risk_pct / downside_var_pct

    # Apply caps
    size_mult = max(min_mult, min(max_mult, size_mult))

    return size_mult


def format_var_summary(var_result: VaRResult, timescale: str = "15m") -> str:
    """
    Format VaR result as a human-readable summary string.

    Args:
        var_result: VaR calculation result
        timescale: Timescale name for display

    Returns:
        Formatted summary string
    """
    return (
        f"VaR Summary ({timescale}): "
        f"downside={var_result.downside_var_pct * 100:.2f}% "
        f"upside={var_result.upside_var_pct * 100:.2f}% "
        f"range={var_result.expected_move_pct * 100:.2f}% "
        f"var_bps={var_result.var_bps} "
        f"size_mult={var_result.position_size_mult:.2f}x"
    )
