"""Real-P&L math — single source of truth.

Replaces the corrupt `pnl_usd` column in `trades` (see memory
`feedback_wallet_truth_authority.md`). Used by both the comparison rollup
use case and (future refactor) by `scripts/ops/wallet_truth.py`.

Pure functions, no IO.
"""

from __future__ import annotations

POLYMARKET_CRYPTO_FEE_MULT = 0.072  # 7.2% — see engine/config/constants.py


def real_pnl_win(fill_price: float, stake_usd: float) -> float:
    """P&L of a winning bet, after Polymarket crypto fees.

    payoff_per_share = 1.0
    shares = stake / fill_price
    gross_payoff = shares * (1 - fill_price)
    fee = POLYMARKET_CRYPTO_FEE_MULT * stake_usd
    net = gross_payoff - fee

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")


def real_pnl_loss(stake_usd: float) -> float:
    """P&L of a losing bet — negative stake.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion. Pure math.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")
