#!/usr/bin/env python3
"""
Polymarket 5-Minute BTC Up/Down Backtest
Compares Pure Delta vs VPIN Enhanced strategies using REAL Binance data.

Key Changes:
- Use FIXED position size ($250 = 25% of initial $1000), not recalculating
- Simpler outcome determination
- More realistic win rates
"""

import aiohttp
import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ============================================================================
# DATA COLLECTION
# ============================================================================

async def fetch_1m_candles(days=14):
    """Fetch 1-minute BTC/USDT candles from Binance API."""
    url = "https://api.binance.com/api/v3/klines"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    candles = []
    current = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    
    async with aiohttp.ClientSession() as session:
        while current < end_ms:
            params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": current, "limit": 1000}
            async with session.get(url, params=params) as resp:
                data = await resp.json()
            if not data:
                break
            for c in data:
                candles.append({
                    "ts": c[0] // 1000,  # unix seconds
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                    "taker_buy_vol": float(c[9]),
                })
            current = data[-1][0] + 1
            await asyncio.sleep(0.2)
    
    print(f"✓ Fetched {len(candles)} 1-minute candles ({days} days)")
    return candles

# ============================================================================
# TOKEN PRICING MODEL
# ============================================================================

def get_token_cost(delta_pct, entry_offset=10):
    """
    Token cost based on delta % and entry timing.
    
    At T-10s (market mostly priced in):
    delta < 0.005%  → $0.50
    delta ~ 0.02%   → $0.55
    delta ~ 0.05%   → $0.65
    delta ~ 0.10%   → $0.80
    delta ~ 0.15%   → $0.92
    
    At T-30s: reduce premium by 40% (earlier entry, cheaper tokens)
    """
    tiers = [
        (0.005, 0.50),
        (0.02, 0.55),
        (0.05, 0.65),
        (0.10, 0.80),
        (0.15, 0.92),
        (0.20, 0.97),
    ]
    
    abs_delta = abs(delta_pct)
    
    if abs_delta <= tiers[0][0]:
        base_cost = tiers[0][1]
    elif abs_delta >= tiers[-1][0]:
        base_cost = tiers[-1][1]
    else:
        for i in range(len(tiers) - 1):
            if tiers[i][0] <= abs_delta <= tiers[i+1][0]:
                t1, p1 = tiers[i]
                t2, p2 = tiers[i+1]
                ratio = (abs_delta - t1) / (t2 - t1)
                base_cost = p1 + ratio * (p2 - p1)
                break
    
    if entry_offset == 30:
        # At T-30s, tokens are cheaper
        return 0.50 + (base_cost - 0.50) * 0.60
    else:
        return base_cost

# ============================================================================
# VPIN CALCULATION
# ============================================================================

def calculate_vpin(candles, window_size=20):
    """Calculate VPIN over a rolling window."""
    vpin_values = []
    
    for i in range(window_size - 1, len(candles)):
        window = candles[i - window_size + 1:i + 1]
        
        total_volume = sum(c["volume"] for c in window)
        if total_volume == 0:
            vpin_values.append(0.5)
            continue
        
        vpin_candles = []
        for c in window:
            if c["volume"] > 0:
                buy_fraction = c["taker_buy_vol"] / c["volume"]
                vpin_candle = abs(buy_fraction - 0.5) * 2
                vpin_candles.append(vpin_candle)
        
        avg_vpin = sum(vpin_candles) / len(vpin_candles) if vpin_candles else 0.5
        vpin_values.append(avg_vpin)
    
    return vpin_values

# ============================================================================
# BACKTEST
# ============================================================================

def run_backtest(candles, vpin_values, strategy="A"):
    """
    Run backtest on all 5-minute windows.
    
    Window structure:
    - T = window start (aligned to 300s)
    - T+300 = window end (resolve timestamp)
    - T+290 = T-10s point (10 seconds before resolution)
    - T+270 = T-30s point (30 seconds before resolution)
    
    Prediction logic:
    - Strategy A: At T+290, bet on current_price vs open_price momentum
    - Strategy B: At T+270, check VPIN; if strong signal, bet; else wait for T+290
    
    Outcome:
    - Actual outcome based on close_price >= open_price
    """
    trades = []
    equity = 1000.0
    position_size = 250.0  # Fixed 25% of starting bankroll
    max_equity = 1000.0
    daily_pnl = defaultdict(float)
    
    # Build candle map
    candle_map = {c["ts"]: c for c in candles}
    
    # Find all complete 5-minute windows
    window_starts = set()
    for c in candles:
        window_start = (c["ts"] // 300) * 300
        window_starts.add(window_start)
    
    for window_start in sorted(window_starts):
        # Get window boundaries
        window_open_ts = window_start
        entry_10_ts = window_start + 290
        entry_30_ts = window_start + 270
        window_close_ts = window_start + 300
        
        # Check we have necessary candles
        if window_open_ts not in candle_map:
            continue
        
        # Find close price (use first candle at or after window_close_ts)
        close_candle = None
        for ts in range(window_close_ts, window_close_ts + 120, 60):
            if ts in candle_map:
                close_candle = candle_map[ts]
                break
        
        if not close_candle:
            continue
        
        open_price = candle_map[window_open_ts]["open"]
        close_price = close_candle["close"]
        actual_up = close_price >= open_price
        
        # Get prices at entry points
        entry_10_candle = candle_map.get(entry_10_ts) or get_candle_at_or_before(candles, entry_10_ts)
        entry_30_candle = candle_map.get(entry_30_ts) or get_candle_at_or_before(candles, entry_30_ts)
        
        if not entry_10_candle or not entry_30_candle:
            continue
        
        price_at_10 = entry_10_candle["close"]
        price_at_30 = entry_30_candle["close"]
        
        delta_at_10 = ((price_at_10 - open_price) / open_price) * 100
        delta_at_30 = ((price_at_30 - open_price) / open_price) * 100
        
        # Get VPIN value for this window
        # Find candle index for VPIN lookup
        vpin_idx = None
        for idx, c in enumerate(candles):
            if c["ts"] == window_start:
                vpin_idx = idx
                break
        
        if vpin_idx is None:
            continue
        
        # VPIN at T-30s is roughly 45 candles into a 300-second window
        vpin_lookup_idx = min(vpin_idx + 44, len(vpin_values) - 1)
        vpin_value = vpin_values[vpin_lookup_idx]
        
        # Strategy logic
        trade_direction = None  # True = Up, False = Down
        entry_point = None
        entry_offset = None
        confidence = 0.0
        
        if strategy == "A":
            # Pure Delta at T-10s
            if abs(delta_at_10) >= 0.03:  # 30% confidence minimum
                entry_point = entry_10_ts
                entry_offset = 10
                trade_direction = delta_at_10 > 0
                confidence = min(abs(delta_at_10) / 0.15, 1.0)
        
        else:  # Strategy B: VPIN Enhanced
            # Check VPIN alignment at T-30s
            vpin_buy_pressure = vpin_value > 0.5
            vpin_strong = vpin_value >= 0.30
            vpin_aligned = vpin_strong and \
                          ((vpin_buy_pressure and delta_at_30 > 0) or \
                           (not vpin_buy_pressure and delta_at_30 < 0))
            
            if vpin_aligned and abs(delta_at_30) >= 0.03:
                # Enter early at T-30s with VPIN edge
                entry_point = entry_30_ts
                entry_offset = 30
                trade_direction = delta_at_30 > 0
                confidence = min(abs(delta_at_30) / 0.15, 1.0) * (1.0 + vpin_value)
            elif abs(delta_at_10) >= 0.03:
                # Fallback to pure delta at T-10s
                entry_point = entry_10_ts
                entry_offset = 10
                trade_direction = delta_at_10 > 0
                confidence = min(abs(delta_at_10) / 0.15, 1.0)
        
        if trade_direction is None:
            continue
        
        # Calculate P&L
        delta_at_entry = delta_at_30 if entry_offset == 30 else delta_at_10
        token_cost = get_token_cost(delta_at_entry, entry_offset)
        
        # How many tokens can we buy with our position size?
        tokens_bought = position_size / token_cost
        
        # Determine if trade wins
        trade_wins = (trade_direction == actual_up)
        
        # Calculate profit
        if trade_wins:
            payout = tokens_bought * 1.0  # Each token pays $1 when it wins
            profit = payout - position_size
        else:
            profit = -position_size
        
        equity += profit
        
        # Track max equity for drawdown
        if equity > max_equity:
            max_equity = equity
        
        # Kill switch: stop trading if drawdown >= 45%
        drawdown_pct = (max_equity - equity) / max_equity * 100
        if drawdown_pct >= 45.0:
            break
        
        # Record trade
        trade = {
            "ts": window_start,
            "date": datetime.fromtimestamp(window_start, tz=timezone.utc).date().isoformat(),
            "direction": "Up" if trade_direction else "Down",
            "actual": "Up" if actual_up else "Down",
            "delta_at_entry": delta_at_entry,
            "vpin": vpin_value,
            "token_cost": token_cost,
            "win": trade_wins,
            "profit": profit,
            "equity_after": equity,
        }
        trades.append(trade)
        daily_pnl[trade["date"]] += profit
    
    return trades, equity, max_equity, daily_pnl

def get_candle_at_or_before(candles, target_ts):
    """Get candle at or before a timestamp."""
    for i in range(len(candles) - 1, -1, -1):
        if candles[i]["ts"] <= target_ts:
            return candles[i]
    return None

# ============================================================================
# METRICS
# ============================================================================

def calc_metrics(trades, final_equity, max_equity):
    """Calculate performance metrics."""
    if not trades:
        return None
    
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["win"])
    win_rate = (wins / total_trades) * 100
    
    pnl = final_equity - 1000.0
    ret_pct = (pnl / 1000.0) * 100
    
    drawdown = (max_equity - final_equity) / max_equity * 100
    
    # Profit factor
    wins_total = sum(t["profit"] for t in trades if t["profit"] > 0)
    losses_total = abs(sum(t["profit"] for t in trades if t["profit"] < 0))
    pf = wins_total / losses_total if losses_total > 0 else (float('inf') if wins_total > 0 else 0)
    
    # Avg win/loss
    winning_trades = [t["profit"] for t in trades if t["profit"] > 0]
    losing_trades = [t["profit"] for t in trades if t["profit"] < 0]
    avg_win = sum(winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = abs(sum(losing_trades) / len(losing_trades)) if losing_trades else 0
    
    return {
        "trades": total_trades,
        "win_rate": win_rate,
        "pnl": pnl,
        "return": ret_pct,
        "drawdown": drawdown,
        "pf": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }

# ============================================================================
# MAIN
# ============================================================================

async def main():
    print("=" * 70)
    print("Polymarket 5-Minute BTC Up/Down Backtest")
    print("Pure Delta vs VPIN Enhanced")
    print("=" * 70)
    
    # Fetch data
    print("\n[1/3] Fetching Binance data...")
    candles = await fetch_1m_candles(days=14)
    
    # Calculate VPIN
    print("[2/3] Calculating VPIN...")
    vpin_values = calculate_vpin(candles, window_size=20)
    print(f"✓ Calculated {len(vpin_values)} VPIN values")
    
    # Run backtests
    print("[3/3] Running backtests...")
    trades_a, equity_a, max_eq_a, daily_a = run_backtest(candles, vpin_values, "A")
    trades_b, equity_b, max_eq_b, daily_b = run_backtest(candles, vpin_values, "B")
    
    metrics_a = calc_metrics(trades_a, equity_a, max_eq_a)
    metrics_b = calc_metrics(trades_b, equity_b, max_eq_b)
    
    if not metrics_a or not metrics_b:
        print("Error: No trades executed")
        return
    
    # Print results
    print("\n" + "=" * 70)
    print("STRATEGY A (Pure Delta) vs STRATEGY B (VPIN Enhanced)")
    print("=" * 70)
    print(f"{'Metric':<20} {'Strategy A':<15} {'Strategy B':<15}")
    print("-" * 50)
    print(f"{'Total Trades':<20} {metrics_a['trades']:<15} {metrics_b['trades']:<15}")
    print(f"{'Win Rate':<20} {metrics_a['win_rate']:.1f}% {'':<10} {metrics_b['win_rate']:.1f}%")
    print(f"{'Total P&L':<20} ${metrics_a['pnl']:>10.2f} ${metrics_b['pnl']:>10.2f}")
    print(f"{'Return':<20} {metrics_a['return']:.1f}% {'':<10} {metrics_b['return']:.1f}%")
    print(f"{'Max Drawdown':<20} {metrics_a['drawdown']:.1f}% {'':<10} {metrics_b['drawdown']:.1f}%")
    print(f"{'Profit Factor':<20} {metrics_a['pf']:.2f} {'':<11} {metrics_b['pf']:.2f}")
    print(f"{'Avg Win':<20} ${metrics_a['avg_win']:>10.2f} ${metrics_b['avg_win']:>10.2f}")
    print(f"{'Avg Loss':<20} ${metrics_a['avg_loss']:>10.2f} ${metrics_b['avg_loss']:>10.2f}")
    
    # Daily P&L
    print("\n" + "=" * 70)
    print("DAILY P&L")
    print("=" * 70)
    print(f"{'Date':<12} {'Strategy A':<15} {'Strategy B':<15} {'Diff':<15}")
    print("-" * 57)
    all_days = sorted(set(daily_a.keys()) | set(daily_b.keys()))
    for day in all_days[:14]:
        pa = daily_a.get(day, 0)
        pb = daily_b.get(day, 0)
        print(f"{day:<12} ${pa:>12.2f} ${pb:>12.2f} ${pb-pa:>12.2f}")
    
    # Top trades
    print("\n" + "=" * 70)
    print("TOP 5 BEST TRADES (Strategy B)")
    print("=" * 70)
    top_b = sorted(trades_b, key=lambda t: t["profit"], reverse=True)[:5]
    for i, t in enumerate(top_b, 1):
        print(f"{i}. {t['date']} | {t['direction']}→{t['actual']} | "
              f"Δ {t['delta_at_entry']:+.3f}% | VPIN {t['vpin']:.2f} | "
              f"Cost ${t['token_cost']:.3f} | P&L ${t['profit']:.2f}")
    
    print("\n" + "=" * 70)
    print("TOP 5 WORST TRADES (Strategy B)")
    print("=" * 70)
    worst_b = sorted(trades_b, key=lambda t: t["profit"])[:5]
    for i, t in enumerate(worst_b, 1):
        print(f"{i}. {t['date']} | {t['direction']}→{t['actual']} | "
              f"Δ {t['delta_at_entry']:+.3f}% | VPIN {t['vpin']:.2f} | "
              f"Cost ${t['token_cost']:.3f} | P&L ${t['profit']:.2f}")
    
    # Save JSON
    results = {
        "metadata": {
            "data_source": "Binance BTCUSDT 1m",
            "days": 14,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "strategy_a": {"metrics": metrics_a, "trades": trades_a, "daily_pnl": dict(daily_a)},
        "strategy_b": {"metrics": metrics_b, "trades": trades_b, "daily_pnl": dict(daily_b)},
    }
    
    with open("/root/.openclaw/workspace-novakash/novakash/backtest_5min_comparison.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\n✓ Results saved to backtest_5min_comparison.json")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(main())
