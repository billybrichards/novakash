"""Pure domain logic for TG notification refactor.

Phase A deliverable. Every function here is side-effect free, synchronous,
and depends only on stdlib + engine.domain.alert_values. Easy unit testing:
no mocks, no fixtures beyond literals.

Public API:
  - score_signal_health
  - classify_outcome
  - classify_wallet_delta
  - compute_shadow_outcome
  - relabel_confidence_on_override
  - is_window_stale
  - polymarket_share_payout
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from domain.alert_values import (
    HealthBadge,
    HealthStatus,
    OutcomeQuadrant,
    ShadowRow,
    WalletDeltaKind,
)

__all__ = [
    "score_signal_health",
    "classify_outcome",
    "classify_wallet_delta",
    "compute_shadow_outcome",
    "relabel_confidence_on_override",
    "is_window_stale",
    "polymarket_share_payout",
    "CONFIDENCE_THRESHOLDS",
]


# Canonical map from pg_signal_repo.py:229 — used for float→label fallback.
# Higher score = stronger signal. Bucket by nearest threshold.
CONFIDENCE_THRESHOLDS = {
    "HIGH": 0.85,
    "MODERATE": 0.65,
    "LOW": 0.45,
    "NONE": 0.20,
}


# ---------------------------------------------------------------------------
# Signal health composite
# ---------------------------------------------------------------------------


def score_signal_health(
    vpin: Optional[float],
    p_up: Optional[float],
    p_up_distance: Optional[float],
    sources_agree: Optional[bool],
    confidence_label: Optional[str],
    confidence_override_active: bool,
    eval_band_in_optimal: bool,
    chainlink_feed_age_s: Optional[float],
    max_feed_age_s: float = 30.0,
) -> HealthBadge:
    """Composite health rollup: OK / DEGRADED / UNSAFE plus reason tags.

    Green dims: sources_agree, VPIN in [0.50, 0.85], confidence ≥ LOW,
    eval band OPTIMAL, feed fresh (<max_feed_age_s).

    Amber: 1 dim degraded.
    Red: 2+ dims degraded, OR confidence=NONE without override, OR feed stale.
    """
    amber: list[str] = []
    red: list[str] = []

    # Sources dimension
    if sources_agree is False:
        amber.append("sources:mixed")
    elif sources_agree is None:
        amber.append("sources:unknown")

    # VPIN dimension — healthy band is informed-but-not-cascading
    if vpin is not None:
        if vpin < 0.40:
            amber.append("vpin:low")
        elif vpin > 0.85:
            amber.append("vpin:cascade_risk")

    # P(UP) extremity — very useful to know if outside [0.05, 0.95] (conviction)
    # Not a health flag, just context; skip.

    # Confidence dimension
    if confidence_label is None or confidence_label == "NONE":
        if not confidence_override_active:
            red.append("confidence:none_no_override")
    # LOW doesn't flag amber by itself; strategies already gate on it.

    # Eval band
    if not eval_band_in_optimal:
        amber.append("eval_band:suboptimal")

    # Feed freshness
    if chainlink_feed_age_s is not None and chainlink_feed_age_s > max_feed_age_s:
        red.append(f"feed:stale_{chainlink_feed_age_s:.0f}s")

    # Distance-to-0.5 — if we have p_up but model is basically flat, amber
    if p_up_distance is not None and p_up_distance < 0.10:
        amber.append("p_up:flat")

    reasons = tuple(red + amber)

    if red or len(amber) >= 2:
        return HealthBadge(status=HealthStatus.UNSAFE, reasons=reasons)
    if len(amber) == 1:
        return HealthBadge(status=HealthStatus.DEGRADED, reasons=reasons)
    return HealthBadge(status=HealthStatus.OK, reasons=())


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def classify_outcome(
    predicted: str,
    actual: str,
    pnl_usdc: Decimal,
) -> OutcomeQuadrant:
    """Four-quadrant outcome label: signal skill × P&L cross-product.

    `predicted` and `actual` must be "UP" | "DOWN".
    `pnl_usdc` Decimal: positive = win, non-positive = loss.
    """
    if predicted not in {"UP", "DOWN"}:
        raise ValueError(f"predicted must be UP or DOWN, got {predicted!r}")
    if actual not in {"UP", "DOWN"}:
        raise ValueError(f"actual must be UP or DOWN, got {actual!r}")

    correct = predicted == actual
    won = pnl_usdc > 0

    if correct and won:
        return OutcomeQuadrant.CORRECT_WIN
    if correct and not won:
        return OutcomeQuadrant.CORRECT_LOSS
    if not correct and won:
        return OutcomeQuadrant.WRONG_WIN
    return OutcomeQuadrant.WRONG_LOSS


# ---------------------------------------------------------------------------
# Wallet delta classification
# ---------------------------------------------------------------------------


def classify_wallet_delta(
    amount_usdc: Decimal,
    dest_addr: Optional[str],
    owner_eoas: frozenset[str],
    poly_contracts: frozenset[str],
    redeemer_addr: Optional[str] = None,
) -> WalletDeltaKind:
    """Classify a wallet outflow by destination.

    - Known owner EOA (user's MetaMask, hot wallet) → MANUAL_WITHDRAWAL
    - Polymarket contracts (CTF, NegRisk, adapter) → TRADING_FLOW
    - Redeemer batch address → REDEMPTION
    - Unknown destination → UNEXPECTED (loud)
    - No destination (tx missing / accounting desync) → DRIFT (loud)

    Address comparisons are case-insensitive.
    """
    if dest_addr is None:
        return WalletDeltaKind.DRIFT

    dest_lower = dest_addr.lower()
    owners = {a.lower() for a in owner_eoas}
    polys = {a.lower() for a in poly_contracts}

    if dest_lower in owners:
        return WalletDeltaKind.MANUAL_WITHDRAWAL
    if redeemer_addr is not None and dest_lower == redeemer_addr.lower():
        return WalletDeltaKind.REDEMPTION
    if dest_lower in polys:
        return WalletDeltaKind.TRADING_FLOW
    return WalletDeltaKind.UNEXPECTED


# ---------------------------------------------------------------------------
# Shadow outcome computation
# ---------------------------------------------------------------------------


def polymarket_share_payout(
    shares: float,
    entry_price_cents: float,
    won: bool,
    fee_mult: float = 0.072,
) -> Decimal:
    """Compute P&L for a Polymarket share purchase.

    Winning share pays out $1.00 (less fee multiplier).
    Losing share pays $0.
    Cost basis is shares × entry_price.
    """
    cost = Decimal(str(shares)) * Decimal(str(entry_price_cents))
    if won:
        gross = Decimal(str(shares)) * Decimal("1.00")
        fee = gross * Decimal(str(fee_mult))
        return (gross - fee - cost).quantize(Decimal("0.0001"))
    return (-cost).quantize(Decimal("0.0001"))


def compute_shadow_outcome(
    *,
    timeframe: str,
    strategy_id: str,
    mode: str,
    action: str,
    direction: Optional[str],
    confidence: Optional[str],
    confidence_score: Optional[float],
    entry_price_cents: Optional[float],
    stake_usdc: Decimal,
    actual_direction: str,
    skip_reason: Optional[str] = None,
    fee_mult: float = 0.072,
) -> ShadowRow:
    """Build a ShadowRow for one strategy given a resolved window's actual move.

    For TRADE decisions, computes hypothetical P&L via Polymarket share math.
    For SKIP decisions, emits a skip row with reason.
    """
    if action == "SKIP":
        return ShadowRow(
            timeframe=timeframe,
            strategy_id=strategy_id,
            mode=mode,
            action="SKIP",
            direction=None,
            outcome=None,
            hypothetical_pnl_usdc=None,
            entry_price_cents=None,
            skip_reason=skip_reason,
        )

    if action != "TRADE":
        raise ValueError(f"action must be TRADE or SKIP, got {action!r}")
    if direction is None or entry_price_cents is None:
        raise ValueError("TRADE action requires direction + entry_price_cents")
    if actual_direction not in {"UP", "DOWN"}:
        raise ValueError(f"actual_direction must be UP/DOWN, got {actual_direction!r}")

    won = direction == actual_direction
    # shares = stake / entry_price (fractional shares supported in Polymarket)
    shares = float(stake_usdc) / entry_price_cents if entry_price_cents > 0 else 0.0
    pnl = polymarket_share_payout(
        shares=shares,
        entry_price_cents=entry_price_cents,
        won=won,
        fee_mult=fee_mult,
    )
    outcome = classify_outcome(
        predicted=direction,
        actual=actual_direction,
        pnl_usdc=pnl,
    )
    return ShadowRow(
        timeframe=timeframe,
        strategy_id=strategy_id,
        mode=mode,
        action="TRADE",
        direction=direction,
        outcome=outcome,
        hypothetical_pnl_usdc=pnl,
        entry_price_cents=entry_price_cents,
        skip_reason=None,
    )


# ---------------------------------------------------------------------------
# Confidence display relabeling (fixes conf=NONE bug)
# ---------------------------------------------------------------------------


def relabel_confidence_on_override(
    label: Optional[str],
    score: Optional[float],
    gate_results: Iterable[dict],
) -> str:
    """Produce user-facing confidence label, fixing the conf=NONE bug.

    Rules (in order):
    1. If risk_off_override gate fired AND passed AND label is None/"NONE",
       emit "OVERRIDE:risk_off" — the override bypassed normal confidence.
    2. If label missing but score present, bucket by CONFIDENCE_THRESHOLDS.
    3. If label present and not "NONE", passthrough unchanged.
    4. Fallback "UNKNOWN" when neither label nor score available.

    This is a DISPLAY-LAYER fix. It does NOT mutate the domain decision.
    """
    override_passed = False
    for g in gate_results or ():
        # Accept both dict and object with attr access
        name = g.get("name") if isinstance(g, dict) else getattr(g, "name", None)
        passed = g.get("passed") if isinstance(g, dict) else getattr(g, "passed", None)
        if name and "risk_off" in str(name).lower() and "override" in str(name).lower():
            if passed:
                override_passed = True
                break

    if override_passed and (label is None or label == "NONE"):
        return "OVERRIDE:risk_off"

    if label and label != "NONE":
        return label

    # label is None or "NONE" with no override — fall back to score bucket
    if score is None:
        return "UNKNOWN"
    # Find nearest threshold band (score >= threshold wins)
    for name in ("HIGH", "MODERATE", "LOW", "NONE"):
        if score >= CONFIDENCE_THRESHOLDS[name]:
            return name
    return "NONE"


# ---------------------------------------------------------------------------
# Window freshness
# ---------------------------------------------------------------------------


def is_window_stale(
    window_ts: int,
    duration_secs: int,
    now: datetime,
) -> bool:
    """True iff the window's close_ts has passed at `now`.

    Used to tag alerts with REPLAY when they fire for already-resolved windows.
    """
    if duration_secs <= 0:
        raise ValueError("duration_secs must be positive")
    close_ts = window_ts + duration_secs
    now_unix = int(now.replace(tzinfo=now.tzinfo or timezone.utc).timestamp())
    return now_unix > close_ts
