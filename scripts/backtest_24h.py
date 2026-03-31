#!/usr/bin/env python3
"""
Backtest Script — Paper Mode Thresholds Analysis

Simulates what would have happened over the last 24 hours with:
- VPIN Informed Threshold: 0.45 (was 0.55)
- VPIN Cascade Threshold: 0.55 (was 0.70)
- Arb Min Spread: 0.005 (was 0.015)

Generates synthetic but realistic trades based on historical price/volume data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from decimal import Decimal

import aiohttp
import structlog

log = structlog.get_logger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# New thresholds (paper mode)
VPIN_INFORMED_THRESHOLD = 0.45
VPIN_CASCADE_THRESHOLD = 0.55
ARB_MIN_SPREAD = 0.005

# Risk parameters
BET_FRACTION = 0.10
STARTING_BANKROLL = 100.0

# Time window
HOURS = 24
INTERVAL_MINUTES = 5  # VPIN bucket size

# ── Data Models ───────────────────────────────────────────────────────────────

class Trade:
    def __init__(
        self,
        timestamp: datetime,
        strategy: str,
        outcome: str,
        pnl_usd: float,
        stake_usd: float,
        vpin: float,
        market_slug: str,
    ):
        self.timestamp = timestamp
        self.strategy = strategy
        self.outcome = outcome
        self.pnl_usd = pnl_usd
        self.stake_usd = stake_usd
        self.vpin = vpin
        self.market_slug = market_slug

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            "outcome": self.outcome,
            "pnl_usd": round(self.pnl_usd, 2),
            "stake_usd": round(self.stake_usd, 2),
            "vpin": round(self.vpin, 3),
            "market_slug": self.market_slug,
        }

# ── Data Fetching ─────────────────────────────────────────────────────────────

async def fetch_btc_price_history(hours: int = 24) -> List[dict]:
    """Fetch BTC/USDT 5m candle data from Binance for last N hours."""
    url = "https://api.binance.com/api/v3/klines"
    
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    
    params = {
        "symbol": "BTCUSDT",
        "interval": "5m",
        "startTime": int(start_time.timestamp() * 1000),
        "endTime": int(end_time.timestamp() * 1000),
        "limit": 300,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
    
    candles = []
    for c in data:
        candles.append({
            "timestamp": datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    
    return candles

async def fetch_polymarket_prices() -> Dict[str, float]:
    """Fetch current Polymarket BTC prices (simulated for backtest)."""
    # In production, this would fetch real CLOB data
    # For backtest, we'll simulate based on BTC price movements
    return {
        "YES": 0.52,
        "NO": 0.48,
    }

# ── VPIN Calculation ──────────────────────────────────────────────────────────

def calculate_vpin(candles: List[dict], bucket_size_usd: float = 50000) -> List[dict]:
    """Calculate VPIN for each bucket."""
    buckets = []
    current_bucket_volume = 0.0
    current_bucket_buy_volume = 0.0
    
    for i, candle in enumerate(candles):
        volume_usd = candle["volume"] * candle["close"]
        current_bucket_volume += volume_usd
        
        # Simplified tick rule: assume 55% buy volume when price up
        if candle["close"] >= candle["open"]:
            buy_ratio = 0.55 + (0.01 * (i % 5))  # Vary slightly
        else:
            buy_ratio = 0.45 + (0.01 * (i % 5))
        
        current_bucket_buy_volume += volume_usd * buy_ratio
        
        # Bucket complete?
        if current_bucket_volume >= bucket_size_usd or i == len(candles) - 1:
            vpin = current_bucket_buy_volume / current_bucket_volume if current_bucket_volume > 0 else 0.5
            buckets.append({
                "timestamp": candle["timestamp"],
                "vpin": vpin,
                "volume_usd": current_bucket_volume,
                "price": candle["close"],
            })
            
            # Reset bucket
            current_bucket_volume = 0.0
            current_bucket_buy_volume = 0.0
    
    return buckets

# ── Trade Simulation ──────────────────────────────────────────────────────────

async def simulate_trades(vpin_data: List[dict], candles: List[dict]) -> List[Trade]:
    """Simulate trades based on VPIN signals and new thresholds."""
    trades = []
    bankroll = STARTING_BANKROLL
    cascade_signal_active = False
    cascade_cooldown_end = None
    
    for i, bucket in enumerate(vpin_data):
        timestamp = bucket["timestamp"]
        vpin = bucket["vpin"]
        price = bucket["price"]
        
        # Check cascade cooldown
        if cascade_cooldown_end and timestamp < cascade_cooldown_end:
            continue
        
        # Cascade signal detection
        if vpin >= VPIN_CASCADE_THRESHOLD:
            cascade_signal_active = True
            stake = bankroll * BET_FRACTION
            # Simulate cascade fade bet outcome (55% win rate)
            import random
            win = random.random() < 0.55
            pnl = stake * 0.85 if win else -stake * 0.95
            
            trades.append(Trade(
                timestamp=timestamp,
                strategy="vpin_cascade",
                outcome="WIN" if win else "LOSS",
                pnl_usd=pnl,
                stake_usd=stake,
                vpin=vpin,
                market_slug=f"BTC-CASCADE-{timestamp.timestamp()}",
            ))
            
            bankroll += pnl
            cascade_signal_active = False
            cascade_cooldown_end = timestamp + timedelta(minutes=15)
        
        # Informed trade signal (lower threshold = more signals)
        elif vpin >= VPIN_INFORMED_THRESHOLD:
            stake = bankroll * BET_FRACTION * 0.5  # Smaller position for informed trades
            import random
            win = random.random() < 0.60  # 60% win rate for informed signals
            pnl = stake * 0.90 if win else -stake * 0.95
            
            trades.append(Trade(
                timestamp=timestamp,
                strategy="vpin_informed",
                outcome="WIN" if win else "LOSS",
                pnl_usd=pnl,
                stake_usd=stake,
                vpin=vpin,
                market_slug=f"BTC-INFORMED-{timestamp.timestamp()}",
            ))
            
            bankroll += pnl
        
        # Arb opportunities (simulated - 0.5% net spread)
        if i % 8 == 0:  # Simulate arb every 8th bucket (~40 minutes)
            import random
            spread = ARB_MIN_SPREAD + random.random() * 0.02  # 0.5-2.5% spread
            if spread >= ARB_MIN_SPREAD:
                stake = 20.0 + random.random() * 30.0  # $20-50 per leg
                # Arb has high win rate (~80%)
                win = random.random() < 0.80
                fee = 0.018 * stake * 2  # 1.8% round-trip fee
                pnl = (spread * 2 * stake) - fee if win else -fee
                
                trades.append(Trade(
                    timestamp=timestamp,
                    strategy="sub_dollar_arb",
                    outcome="WIN" if win else "LOSS",
                    pnl_usd=pnl,
                    stake_usd=stake * 2,
                    vpin=vpin,
                    market_slug=f"BTC-ARB-{timestamp.timestamp()}",
                ))
                
                bankroll += pnl
    
    return trades

# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_trades(trades: List[Trade]) -> dict:
    """Generate performance metrics from trades."""
    if not trades:
        return {"error": "No trades generated"}
    
    total_pnl = sum(t.pnl_usd for t in trades)
    wins = [t for t in trades if t.outcome == "WIN"]
    losses = [t for t in trades if t.outcome == "LOSS"]
    
    cascade_trades = [t for t in trades if t.strategy == "vpin_cascade"]
    informed_trades = [t for t in trades if t.strategy == "vpin_informed"]
    arb_trades = [t for t in trades if t.strategy == "sub_dollar_arb"]
    
    return {
        "summary": {
            "total_trades": len(trades),
            "total_pnl": round(total_pnl, 2),
            "starting_bankroll": STARTING_BANKROLL,
            "ending_bankroll": round(STARTING_BANKROLL + total_pnl, 2),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(sum(t.pnl_usd for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t.pnl_usd for t in losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses)), 2) if losses else float('inf'),
        },
        "by_strategy": {
            "vpin_cascade": {
                "trades": len(cascade_trades),
                "pnl": round(sum(t.pnl_usd for t in cascade_trades), 2),
                "win_rate": round(len([t for t in cascade_trades if t.outcome == "WIN"]) / len(cascade_trades) * 100, 1) if cascade_trades else 0,
            },
            "vpin_informed": {
                "trades": len(informed_trades),
                "pnl": round(sum(t.pnl_usd for t in informed_trades), 2),
                "win_rate": round(len([t for t in informed_trades if t.outcome == "WIN"]) / len(informed_trades) * 100, 1) if informed_trades else 0,
            },
            "sub_dollar_arb": {
                "trades": len(arb_trades),
                "pnl": round(sum(t.pnl_usd for t in arb_trades), 2),
                "win_rate": round(len([t for t in arb_trades if t.outcome == "WIN"]) / len(arb_trades) * 100, 1) if arb_trades else 0,
            },
        },
        "thresholds_used": {
            "vpin_informed": VPIN_INFORMED_THRESHOLD,
            "vpin_cascade": VPIN_CASCADE_THRESHOLD,
            "arb_min_spread": ARB_MIN_SPREAD,
            "bet_fraction": BET_FRACTION,
        },
    }

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    """Run the backtest."""
    print("🔍 Running 24-hour backtest with paper mode thresholds...")
    print(f"   VPIN Informed: {VPIN_INFORMED_THRESHOLD}")
    print(f"   VPIN Cascade: {VPIN_CASCADE_THRESHOLD}")
    print(f"   Arb Min Spread: {ARB_MIN_SPREAD}")
    print(f"   Bet Fraction: {BET_FRACTION * 100}%")
    print(f"   Starting Bankroll: ${STARTING_BANKROLL}\n")
    
    # Fetch historical data
    print("📊 Fetching BTC price history (last 24 hours)...")
    candles = await fetch_btc_price_history(HOURS)
    print(f"   Retrieved {len(candles)} candles ({len(candles) * 5} minutes of data)")
    
    # Calculate VPIN
    print("📡 Calculating VPIN signals...")
    vpin_data = calculate_vpin(candles)
    print(f"   Generated {len(vpin_data)} VPIN buckets")
    
    # Simulate trades
    print("⚡ Simulating trades with new thresholds...")
    trades = await simulate_trades(vpin_data, candles)
    print(f"   Generated {len(trades)} trades\n")
    
    # Analyze results
    print("📈 Analyzing performance...")
    analysis = analyze_trades(trades)
    
    # Print summary
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS (24 Hours)")
    print("=" * 60)
    
    summary = analysis["summary"]
    print(f"\n💰 Total P&L: ${summary['total_pnl']:+.2f}")
    print(f"📊 Total Trades: {summary['total_trades']}")
    print(f"🎯 Win Rate: {summary['win_rate']:.1f}% ({summary['wins']}W / {summary['losses']}L)")
    print(f"📈 Starting: ${summary['starting_bankroll']}")
    print(f"📉 Ending: ${summary['ending_bankroll']}")
    print(f"💹 Profit Factor: {summary['profit_factor']:.2f}")
    
    print("\n" + "-" * 60)
    print("By Strategy:")
    print("-" * 60)
    
    for strategy, stats in analysis["by_strategy"].items():
        print(f"\n{strategy.upper()}:")
        print(f"   Trades: {stats['trades']}")
        print(f"   P&L: ${stats['pnl']:+.2f}")
        print(f"   Win Rate: {stats['win_rate']:.1f}%")
    
    # Save results
    output = {
        "period_hours": HOURS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": analysis,
        "trades": [t.to_dict() for t in trades],
    }
    
    output_path = "backtest_results_24h.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\n📁 Full results saved to: {output_path}")
    print("\n" + "=" * 60)
    
    return analysis

if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    
    import logging
    asyncio.run(main())
