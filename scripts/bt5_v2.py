#!/usr/bin/env python3
"""
5-Min BTC Up/Down Backtest v2
- Uses 4th candle CLOSE as T-10s proxy (not open)
- Analyzes delta distribution first
- Proper 25% Kelly with drawdown management
"""
import asyncio, aiohttp, json
from datetime import datetime, timedelta, timezone
from collections import deque

STARTING = 1000.0
BET_PCT = 0.25
MAX_DD = 0.45
DAYS = 7

def token_price(delta_pct):
    """Realistic Polymarket token pricing based on gist observations."""
    d = abs(delta_pct)
    if d < 0.005: return 0.50
    elif d < 0.02: return 0.50 + (d - 0.005) / 0.015 * 0.05
    elif d < 0.05: return 0.55 + (d - 0.02) / 0.03 * 0.10
    elif d < 0.10: return 0.65 + (d - 0.05) / 0.05 * 0.15
    elif d < 0.15: return 0.80 + (d - 0.10) / 0.05 * 0.12
    else: return min(0.92 + (d - 0.15) / 0.10 * 0.05, 0.97)

async def fetch_1m(days):
    url = "https://api.binance.com/api/v3/klines"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    candles = []
    cur = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    async with aiohttp.ClientSession() as session:
        while cur < end_ms:
            params = {"symbol":"BTCUSDT","interval":"1m","startTime":cur,"limit":1000}
            async with session.get(url, params=params) as resp:
                data = await resp.json()
            if not data: break
            for c in data:
                candles.append({"ts":c[0]//1000,"open":float(c[1]),"high":float(c[2]),"low":float(c[3]),"close":float(c[4]),"volume":float(c[5]),"taker_buy":float(c[9])})
            cur = data[-1][0] + 1
            await asyncio.sleep(0.15)
    return candles

def build_windows(candles):
    by_ts = {c["ts"]: c for c in candles}
    windows = []
    all_ts = sorted(by_ts.keys())
    if not all_ts: return []
    start = (all_ts[0] // 300) * 300
    end = (all_ts[-1] // 300) * 300
    ts = start
    while ts <= end:
        wc = []
        for offset in range(0, 300, 60):
            t = ts + offset
            if t in by_ts: wc.append(by_ts[t])
        if len(wc) == 5:
            op = wc[0]["open"]
            cl = wc[4]["close"]
            # T-10s proxy: close of 4th candle (timestamp: window+240, so 4:00-4:59)
            # This is the price at ~4:59 into the window, 1 second before the 5th candle opens
            t10_price = wc[3]["close"]
            
            tv = sum(c["volume"]*c["close"] for c in wc)
            bv = sum(c["taker_buy"]*c["close"] for c in wc)
            bp = bv/tv if tv > 0 else 0.5
            outcome = "UP" if cl >= op else "DOWN"
            
            # Delta at T-10s
            delta = (t10_price - op) / op * 100
            
            windows.append({
                "ts": ts,
                "open": op,
                "close": cl,
                "t10": t10_price,
                "delta": delta,
                "outcome": outcome,
                "buy_pct": bp,
                "vol": tv,
            })
        ts += 300
    return windows

def calc_vpin(windows, lookback=20):
    buf = deque(maxlen=lookback)
    for w in windows:
        buf.append(w["buy_pct"])
        w["vpin"] = sum(abs(b-0.5)*2 for b in buf)/len(buf) if len(buf) >= 5 else 0.0
    return windows

def accuracy_at_t10(windows):
    """How often does T-10s delta correctly predict the outcome?"""
    correct = 0
    total = 0
    by_bucket = {}
    
    for w in windows:
        d = abs(w["delta"])
        predicted = "UP" if w["delta"] > 0 else "DOWN"
        
        if d < 0.001:  # Skip near-zero
            continue
        
        total += 1
        if predicted == w["outcome"]:
            correct += 1
        
        # Bucket by delta magnitude
        if d < 0.005: bucket = "<0.005%"
        elif d < 0.01: bucket = "0.005-0.01%"
        elif d < 0.02: bucket = "0.01-0.02%"
        elif d < 0.05: bucket = "0.02-0.05%"
        elif d < 0.10: bucket = "0.05-0.10%"
        else: bucket = ">0.10%"
        
        if bucket not in by_bucket:
            by_bucket[bucket] = {"total": 0, "correct": 0}
        by_bucket[bucket]["total"] += 1
        if predicted == w["outcome"]:
            by_bucket[bucket]["correct"] += 1
    
    return correct, total, by_bucket

def run_strategy(windows, name, use_vpin=False, min_delta=0.005):
    """Run a trading strategy."""
    bank = STARTING
    peak = STARTING
    trades = []
    daily_pnl = {}
    
    for w in windows:
        if bank <= 50:
            break
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd >= MAX_DD:
            break
        
        delta = w["delta"]
        vpin = w.get("vpin", 0)
        
        if abs(delta) < min_delta:
            continue
        
        direction = "UP" if delta > 0 else "DOWN"
        
        # VPIN enhancement
        vpin_boost = False
        if use_vpin:
            aligned = (vpin >= 0.30 and 
                      ((w["buy_pct"] > 0.55 and direction == "UP") or
                       (w["buy_pct"] < 0.45 and direction == "DOWN")))
            if aligned:
                vpin_boost = True
                cost = max(token_price(delta) * 0.80, 0.45)
            else:
                cost = token_price(delta)
        else:
            cost = token_price(delta)
        
        bet = bank * BET_PCT
        shares = bet / cost
        
        win = direction == w["outcome"]
        if win:
            profit = shares * (1.0 - cost)
            bank += profit
        else:
            bank -= bet
        
        peak = max(peak, bank)
        
        day = datetime.fromtimestamp(w["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = 0
        daily_pnl[day] += (shares * (1.0 - cost)) if win else -bet
        
        trades.append({
            "win": win, "delta": round(delta, 5), "cost": round(cost, 3),
            "bank": round(bank, 2), "vpin": round(vpin, 4), "boost": vpin_boost,
            "ts": w["ts"],
        })
    
    return trades, bank, daily_pnl

async def main():
    print("=" * 70)
    print("5-MIN BTC UP/DOWN BACKTEST v2 — 7 DAYS REAL DATA")
    print("=" * 70)
    
    print(f"\n📊 Fetching {DAYS}d of 1-min candles...")
    candles = await fetch_1m(DAYS)
    print(f"   {len(candles)} candles")
    
    print(f"🔨 Building 5-min windows...")
    windows = build_windows(candles)
    print(f"   {len(windows)} complete windows")
    
    up = sum(1 for w in windows if w["outcome"] == "UP")
    print(f"   Market: {up/len(windows)*100:.1f}% UP / {(1-up/len(windows))*100:.1f}% DOWN")
    
    # Delta distribution
    deltas = [abs(w["delta"]) for w in windows]
    print(f"\n📐 Delta Distribution (T-10s vs open):")
    print(f"   Min: {min(deltas):.5f}% | Max: {max(deltas):.5f}% | Mean: {sum(deltas)/len(deltas):.5f}%")
    for thresh in [0.001, 0.005, 0.01, 0.02, 0.05, 0.10]:
        count = sum(1 for d in deltas if d >= thresh)
        print(f"   >= {thresh:.3f}%: {count} windows ({count/len(windows)*100:.1f}%)")
    
    # Accuracy analysis
    correct, total, by_bucket = accuracy_at_t10(windows)
    print(f"\n🎯 T-10s Prediction Accuracy:")
    print(f"   Overall: {correct}/{total} = {correct/total*100:.1f}%")
    print(f"\n   By delta magnitude:")
    for bucket in ["<0.005%", "0.005-0.01%", "0.01-0.02%", "0.02-0.05%", "0.05-0.10%", ">0.10%"]:
        if bucket in by_bucket:
            b = by_bucket[bucket]
            print(f"   {bucket:>12}: {b['correct']}/{b['total']} = {b['correct']/b['total']*100:.1f}% accurate")
    
    # VPIN
    windows = calc_vpin(windows)
    vpins = [w["vpin"] for w in windows if w["vpin"] > 0]
    print(f"\n📡 VPIN: {min(vpins):.3f}-{max(vpins):.3f} (mean {sum(vpins)/len(vpins):.3f})")
    
    # Run strategies with different min_delta thresholds
    print(f"\n{'=' * 70}")
    print(f"STRATEGY COMPARISON")
    print(f"{'=' * 70}")
    
    # Find optimal delta threshold
    print(f"\n📊 Strategy A (Pure Delta) — Sweeping min_delta:")
    print(f"{'min_delta':>10} {'Trades':>7} {'WR':>7} {'P&L':>12} {'MaxDD':>7}")
    print(f"{'─' * 50}")
    
    best_a = None
    for md in [0.001, 0.002, 0.005, 0.01, 0.02, 0.05]:
        ta, fa, da = run_strategy(windows, "A", use_vpin=False, min_delta=md)
        if not ta: continue
        wa = sum(1 for t in ta if t["win"])
        pa = fa - STARTING
        mda = max((max(ta[j]["bank"] for j in range(i+1)) - ta[i]["bank"]) / max(ta[j]["bank"] for j in range(i+1)) for i in range(len(ta))) if ta else 0
        print(f"{md:>10.3f} {len(ta):>7} {wa/len(ta)*100:>6.1f}% ${pa:>+10.2f} {mda*100:>6.1f}%")
        if best_a is None or pa > best_a[1]:
            best_a = (md, pa, ta, fa, da)
    
    print(f"\n📊 Strategy B (VPIN Enhanced) — Sweeping min_delta:")
    print(f"{'min_delta':>10} {'Trades':>7} {'WR':>7} {'P&L':>12} {'MaxDD':>7} {'Boosts':>7}")
    print(f"{'─' * 60}")
    
    best_b = None
    for md in [0.001, 0.002, 0.005, 0.01, 0.02, 0.05]:
        tb, fb, db = run_strategy(windows, "B", use_vpin=True, min_delta=md)
        if not tb: continue
        wb = sum(1 for t in tb if t["win"])
        pb = fb - STARTING
        mdb = max((max(tb[j]["bank"] for j in range(i+1)) - tb[i]["bank"]) / max(tb[j]["bank"] for j in range(i+1)) for i in range(len(tb))) if tb else 0
        boosts = sum(1 for t in tb if t.get("boost"))
        print(f"{md:>10.3f} {len(tb):>7} {wb/len(tb)*100:>6.1f}% ${pb:>+10.2f} {mdb*100:>6.1f}% {boosts:>7}")
        if best_b is None or pb > best_b[1]:
            best_b = (md, pb, tb, fb, db)
    
    # Best configs comparison
    if best_a and best_b:
        print(f"\n{'=' * 70}")
        print(f"BEST CONFIGS COMPARISON")
        print(f"{'=' * 70}")
        
        md_a, pa, ta, fa, da = best_a
        md_b, pb, tb, fb, db = best_b
        wa = sum(1 for t in ta if t["win"])
        wb = sum(1 for t in tb if t["win"])
        boosts = sum(1 for t in tb if t.get("boost"))
        
        print(f"\n{'Metric':<25} {'A (delta={:.3f})'.format(md_a):>20} {'B (delta={:.3f})'.format(md_b):>20}")
        print(f"{'─' * 65}")
        print(f"{'Total Trades':<25} {len(ta):>20} {len(tb):>20}")
        print(f"{'Win Rate':<25} {f'{wa/len(ta)*100:.1f}%':>20} {f'{wb/len(tb)*100:.1f}%':>20}")
        print(f"{'P&L':<25} {f'${pa:+.2f}':>20} {f'${pb:+.2f}':>20}")
        print(f"{'Return':<25} {f'{pa/STARTING*100:+.1f}%':>20} {f'{pb/STARTING*100:+.1f}%':>20}")
        print(f"{'Final Bankroll':<25} {f'${fa:.2f}':>20} {f'${fb:.2f}':>20}")
        print(f"{'VPIN Boosts':<25} {'N/A':>20} {boosts:>20}")
        
        # Daily
        print(f"\nDaily P&L (best config):")
        all_days = sorted(set(list(da.keys()) + list(db.keys())))
        for day in all_days:
            a = da.get(day, 0)
            b = db.get(day, 0)
            print(f"  {day}: A=${a:>+9.2f}  B=${b:>+9.2f}  {'🟢B' if b > a else '🔴A' if a > b else '⚪'}")
    
    # Save
    output = {
        "days": DAYS,
        "windows": len(windows),
        "accuracy": {b: {"correct": v["correct"], "total": v["total"], "pct": round(v["correct"]/v["total"]*100, 1)} for b, v in by_bucket.items()},
    }
    with open("/root/.openclaw/workspace-novakash/novakash/backtest_5min_comparison.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\n📁 Saved: backtest_5min_comparison.json")
    print(f"{'=' * 70}")

asyncio.run(main())
