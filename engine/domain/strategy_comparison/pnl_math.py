"""Real-P&L math — single source of truth.

Replaces the corrupt `pnl_usd` column in `trades` (see memory
`feedback_wallet_truth_authority.md`). Used by both the comparison rollup
use case and (future refactor) by `scripts/ops/wallet_truth.py`.

Pure functions, no IO.
"""

from __future__ import annotations

import math

# Mirrors engine/config/constants.py::POLYMARKET_CRYPTO_FEE_MULT.
# Domain layer stays pure — no config imports — so we keep a local copy.
POLYMARKET_CRYPTO_FEE_MULT = 0.072  # 7.2%


def real_pnl_win(fill_price: float, stake_usd: float) -> float:
    """P&L of a winning bet, after Polymarket crypto fees.

    shares = stake / fill_price
    gross_payoff = shares * (1 - fill_price)
    net = gross_payoff - fee_rate * stake
    """
    if fill_price <= 0 or fill_price >= 1:
        raise ValueError(f"fill_price must be in (0, 1), got {fill_price}")
    if stake_usd <= 0:
        raise ValueError(f"stake_usd must be positive, got {stake_usd}")
    shares = stake_usd / fill_price
    gross = shares * (1.0 - fill_price)
    fee = POLYMARKET_CRYPTO_FEE_MULT * stake_usd
    return gross - fee


def real_pnl_loss(stake_usd: float) -> float:
    """P&L of a losing bet — negative stake."""
    if stake_usd <= 0:
        raise ValueError(f"stake_usd must be positive, got {stake_usd}")
    return -stake_usd


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion. Pure math."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    spread = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denominator
    return (max(0.0, center - spread), min(1.0, center + spread))
