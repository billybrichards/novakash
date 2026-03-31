#!/usr/bin/env python3
"""
Download historical Binance Futures data for backtesting.

Fetches:
- aggTrades (tick-level trade data)
- klines (OHLCV candles)

Data is saved to data/ directory as CSV files.

Usage:
    python scripts/fetch_history.py --days 30
    python scripts/fetch_history.py --start 2024-01-01 --end 2024-03-01
"""

import argparse
import asyncio
import csv
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

BINANCE_BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
OUTPUT_DIR = Path("data/historical")


async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str,
                       start_ms: int, end_ms: int) -> list:
    """Fetch kline/candlestick data from Binance Futures."""
    url = f"{BINANCE_BASE}/fapi/v1/klines"
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1500,
        }
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if not data:
                break
            all_klines.extend(data)
            current_start = data[-1][6] + 1  # Close time + 1ms
            await asyncio.sleep(0.1)  # Rate limit

    return all_klines


async def fetch_agg_trades(session: aiohttp.ClientSession, symbol: str,
                           start_ms: int, end_ms: int) -> list:
    """Fetch aggregate trade data from Binance Futures."""
    url = f"{BINANCE_BASE}/fapi/v1/aggTrades"
    all_trades = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "startTime": current_start,
            "endTime": min(current_start + 3600000, end_ms),  # 1hr chunks
            "limit": 1000,
        }
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if not data:
                break
            all_trades.extend(data)
            current_start = data[-1]["T"] + 1
            await asyncio.sleep(0.1)

    return all_trades


def save_klines(klines: list, filepath: Path):
    """Save klines to CSV."""
    headers = ["open_time", "open", "high", "low", "close", "volume",
               "close_time", "quote_volume", "trades", "taker_buy_vol",
               "taker_buy_quote_vol", "ignore"]
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(klines)
    print(f"  Saved {len(klines)} klines to {filepath}")


def save_trades(trades: list, filepath: Path):
    """Save aggTrades to CSV."""
    headers = ["agg_id", "price", "quantity", "first_trade_id",
               "last_trade_id", "timestamp", "is_buyer_maker"]
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for t in trades:
            writer.writerow([t["a"], t["p"], t["q"], t["f"], t["l"], t["T"], t["m"]])
    print(f"  Saved {len(trades)} trades to {filepath}")


async def main():
    parser = argparse.ArgumentParser(description="Fetch Binance historical data")
    parser.add_argument("--days", type=int, default=30, help="Days of history to fetch")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--trades", action="store_true", help="Also fetch aggTrades (slow)")
    args = parser.parse_args()

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
    else:
        end = datetime.utcnow()
        start = end - timedelta(days=args.days)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    print(f"Fetching {SYMBOL} data from {start.date()} to {end.date()}")

    async with aiohttp.ClientSession() as session:
        # Klines (1-minute)
        print("\nFetching 1m klines...")
        klines = await fetch_klines(session, SYMBOL, "1m", start_ms, end_ms)
        save_klines(klines, OUTPUT_DIR / f"{SYMBOL}_1m_{start.date()}_{end.date()}.csv")

        # Klines (5-minute)
        print("Fetching 5m klines...")
        klines_5m = await fetch_klines(session, SYMBOL, "5m", start_ms, end_ms)
        save_klines(klines_5m, OUTPUT_DIR / f"{SYMBOL}_5m_{start.date()}_{end.date()}.csv")

        # AggTrades (optional — very large)
        if args.trades:
            print("Fetching aggTrades (this may take a while)...")
            trades = await fetch_agg_trades(session, SYMBOL, start_ms, end_ms)
            save_trades(trades, OUTPUT_DIR / f"{SYMBOL}_aggTrades_{start.date()}_{end.date()}.csv")

    print("\n✅ Done!")


if __name__ == "__main__":
    asyncio.run(main())
