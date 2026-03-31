#!/usr/bin/env python3
"""
Backtest runner for the BTC prediction market strategies.

Replays historical data through VPIN calculator, cascade detector,
and arb scanner to simulate trading performance.

Usage:
    python scripts/backtest.py --data data/historical/BTCUSDT_1m_*.csv
    python scripts/backtest.py --data data/historical/ --strategy vpin_cascade
    python scripts/backtest.py --data data/historical/ --strategy arb
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

# Add engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from config.constants import (  # noqa: E402
    BET_FRACTION,
    VPIN_BUCKET_SIZE_USD,
    VPIN_LOOKBACK_BUCKETS,
    VPIN_CASCADE_THRESHOLD,
    VPIN_INFORMED_THRESHOLD,
    POLYMARKET_CRYPTO_FEE_MULT,
    OPINION_CRYPTO_FEE_MULT,
)


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    strategy: str
    start_date: str
    end_date: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees_paid: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_balance: float = 0.0
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades

    @property
    def sharpe_ratio(self) -> Optional[float]:
        """Approximate Sharpe from daily returns."""
        if len(self.equity_curve) < 2:
            return None
        returns = []
        for i in range(1, len(self.equity_curve)):
            r = (self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1]
            returns.append(r)
        if not returns:
            return None
        import statistics
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns) if len(returns) > 1 else 1
        if std_r == 0:
            return None
        return (mean_r / std_r) * (252 ** 0.5)  # Annualised

    def summary(self) -> str:
        return (
            f"\n{'='*50}\n"
            f"BACKTEST RESULTS: {self.strategy}\n"
            f"{'='*50}\n"
            f"Period:        {self.start_date} → {self.end_date}\n"
            f"Total Trades:  {self.total_trades}\n"
            f"Win Rate:      {self.win_rate*100:.1f}%\n"
            f"Gross P&L:     ${self.gross_pnl:.2f}\n"
            f"Fees Paid:     ${self.fees_paid:.2f}\n"
            f"Net P&L:       ${self.net_pnl:.2f}\n"
            f"Max Drawdown:  {self.max_drawdown*100:.2f}%\n"
            f"Sharpe Ratio:  {self.sharpe_ratio:.2f if self.sharpe_ratio else 'N/A'}\n"
            f"{'='*50}\n"
        )


def load_klines(filepath: Path) -> list[dict]:
    """Load kline CSV into list of dicts."""
    rows = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp": int(row["open_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--data", required=True, help="Path to historical data CSV or directory")
    parser.add_argument("--strategy", choices=["arb", "vpin_cascade", "both"], default="both")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll")
    parser.add_argument("--output", type=str, help="Save results to JSON file")
    args = parser.parse_args()

    data_path = Path(args.data)
    if data_path.is_dir():
        files = sorted(data_path.glob("*_1m_*.csv"))
        if not files:
            print("ERROR: No 1m kline CSVs found in directory")
            sys.exit(1)
        print(f"Found {len(files)} data files")
    else:
        files = [data_path]

    for filepath in files:
        print(f"\nLoading {filepath}...")
        klines = load_klines(filepath)
        print(f"  {len(klines)} candles loaded")

        # TODO: Implement actual backtest logic by replaying klines
        # through VPIN calculator, cascade detector, and arb scanner
        result = BacktestResult(
            strategy=args.strategy,
            start_date=str(datetime.fromtimestamp(klines[0]["timestamp"] / 1000).date()) if klines else "?",
            end_date=str(datetime.fromtimestamp(klines[-1]["timestamp"] / 1000).date()) if klines else "?",
        )

        print(result.summary())

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump({
                    "strategy": result.strategy,
                    "start_date": result.start_date,
                    "end_date": result.end_date,
                    "total_trades": result.total_trades,
                    "win_rate": result.win_rate,
                    "net_pnl": result.net_pnl,
                    "max_drawdown": result.max_drawdown,
                    "sharpe_ratio": result.sharpe_ratio,
                }, f, indent=2)
            print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
