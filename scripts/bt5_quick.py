#!/usr/bin/env python3
"""Quick 7-day backtest: Pure Delta vs VPIN Enhanced on 5-min BTC markets."""
import asyncio, aiohttp, json
from datetime import datetime, timedelta, timezone
from collections import deque

STARTING = 1000.0
BET_PCT = 0.10  # 10% per trade - more conservative to survive drawdowns
MAX_DD = 0.60   # Higher kill switch to let it run
DAYS = 7

def token_price(delta_pct):
    d = abs(delta_pct)
    if d < 0.005: return 0.50
    elif d < 0.02: return 0.50 + (d - 0.005) / (0.02 - 0.005) * 0.05
    elif d < 0.05: return 0.55 + (d - 0.02) / (0.05 - 0.02) * 0.10
    elif d < 0.10: return 0.65 + (d - 0.05) / (0.10 - 0.05) * 0.15
    elif d < 0.15: return 0.80 + (d - 0.10) / (0.15 - 0.10) * 0.12
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
                candles.append({"ts":c[0]//1000,"open":float(c[1]),"close":float(c[4]),"volume":float(c[5]),"taker_buy":float(c[9])})
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
        if len(wc) >= 4:
            op = wc[0]["open"]
            cl = wc[-1]["close"]
            # T-10s price: use the 4th candle's CLOSE (4:50-ish mark)
            # This simulates looking at price with ~10s remaining
            # The 5th candle (4:00-4:59) close would be T-1s which is too late
            t10 = wc[3]["close"] if len(wc) >= 4 else wc[-2]["close"]
            tv = sum(c["volume"]*c["close"] for c in wc)
            bv = sum(c["taker_buy"]*c["close"] for c in wc)
            bp = bv/tv if tv > 0 else 0.5
            outcome = "UP" if cl >= op else "DOWN"
            windows.append({"ts":ts,"open":op,"close":cl,"t10":t10,"outcome":outcome,"buy_pct":bp,"vol":tv})
        ts += 300
    return windows

def calc_vpin(windows, lookback=20):
    buf = deque(maxlen=lookback)
    for w in windows:
        buf.append(w["buy_pct"])
        w["vpin"] = sum(abs(b-0.5)*2 for b in buf)/len(buf) if len(buf)>=5 else 0.0
    return windows

def run_a(windows):
    bank = STARTING; peak = STARTING; trades = []
    for w in windows:
        if bank <= 50: break
        if (peak-bank)/peak >= MAX_DD: break
        delta = (w["t10"]-w["open"])/w["open"]*100
        if abs(delta) < 0.001: continue
        d = "UP" if delta > 0 else "DOWN"
        cost = token_price(delta)
        bet = min(bank * BET_PCT, 50.0)  # Cap at $50 per trade for realistic results
        shares = bet / cost
        if d == w["outcome"]:
            bank += shares * (1.0 - cost)
        else:
            bank -= bet
        peak = max(peak, bank)
        trades.append({"ts":w["ts"],"win":d==w["outcome"],"delta":round(delta,4),"cost":round(cost,3),"bank":round(bank,2)})
    return trades, bank

def run_b(windows):
    bank = STARTING; peak = STARTING; trades = []
    for w in windows:
        if bank <= 50: break
        if (peak-bank)/peak >= MAX_DD: break
        delta = (w["t10"]-w["open"])/w["open"]*100
        vpin = w.get("vpin",0)
        if abs(delta) < 0.001: continue
        d = "UP" if delta > 0 else "DOWN"
        aligned = (vpin >= 0.30 and ((w["buy_pct"]>0.55 and d=="UP") or (w["buy_pct"]<0.45 and d=="DOWN")))
        if aligned:
            cost = max(token_price(delta) * 0.80, 0.45)
        else:
            cost = token_price(delta)
        bet = min(bank * BET_PCT, 50.0)  # Cap at $50 per trade
        shares = bet / cost
        if d == w["outcome"]:
            bank += shares * (1.0 - cost)
        else:
            bank -= bet
        peak = max(peak, bank)
        trades.append({"ts":w["ts"],"win":d==w["outcome"],"delta":round(delta,4),"cost":round(cost,3),"vpin":round(vpin,4),"boost":aligned,"bank":round(bank,2)})
    return trades, bank

async def main():
    print("="*70)
    print("5-MIN BTC UP/DOWN BACKTEST — 7 DAYS REAL DATA")
    print("Pure Delta vs VPIN Enhanced | Realistic Token Pricing")
    print("="*70)
    
    print(f"\n📊 Fetching {DAYS}d of 1-min candles...")
    candles = await fetch_1m(DAYS)
    print(f"   {len(candles)} candles")
    
    print(f"🔨 Building 5-min windows...")
    windows = build_windows(candles)
    print(f"   {len(windows)} windows")
    up = sum(1 for w in windows if w["outcome"]=="UP")
    print(f"   {up/len(windows)*100:.1f}% UP / {(1-up/len(windows))*100:.1f}% DOWN")
    
    windows = calc_vpin(windows)
    vpins = [w["vpin"] for w in windows if w["vpin"]>0]
    print(f"   VPIN: {min(vpins):.3f}-{max(vpins):.3f} (mean {sum(vpins)/len(vpins):.3f})")
    
    print(f"\n⚡ Running strategies...")
    ta, fa = run_a(windows)
    tb, fb = run_b(windows)
    
    wa = sum(1 for t in ta if t["win"])
    wb = sum(1 for t in tb if t["win"])
    boosts = sum(1 for t in tb if t.get("boost"))
    
    pa = fa - STARTING
    pb = fb - STARTING
    
    mda = max((max(t["bank"] for t in ta[:i+1])-t["bank"])/max(t["bank"] for t in ta[:i+1]) for i,t in enumerate(ta)) if ta else 0
    mdb = max((max(t["bank"] for t in tb[:i+1])-t["bank"])/max(t["bank"] for t in tb[:i+1]) for i,t in enumerate(tb)) if tb else 0
    
    print(f"\n{'='*70}")
    print(f"{'RESULTS':^70}")
    print(f"{'='*70}")
    print(f"\n{'Metric':<25} {'A: Pure Delta':>20} {'B: VPIN Enhanced':>20}")
    print(f"{'─'*65}")
    print(f"{'Total Trades':<25} {len(ta):>20} {len(tb):>20}")
    print(f"{'Wins / Losses':<25} {f'{wa}/{len(ta)-wa}':>20} {f'{wb}/{len(tb)-wb}':>20}")
    print(f"{'Win Rate':<25} {f'{wa/len(ta)*100:.1f}%' if ta else 'N/A':>20} {f'{wb/len(tb)*100:.1f}%' if tb else 'N/A':>20}")
    print(f"{'Total P&L':<25} {f'${pa:+.2f}':>20} {f'${pb:+.2f}':>20}")
    print(f"{'Return':<25} {f'{pa/STARTING*100:+.1f}%':>20} {f'{pb/STARTING*100:+.1f}%':>20}")
    print(f"{'Final Bankroll':<25} {f'${fa:.2f}':>20} {f'${fb:.2f}':>20}")
    print(f"{'Max Drawdown':<25} {f'{mda*100:.1f}%':>20} {f'{mdb*100:.1f}%':>20}")
    print(f"{'VPIN Boosts':<25} {'N/A':>20} {boosts:>20}")
    
    # Daily
    print(f"\n{'─'*65}")
    print("Daily P&L:")
    da = {}; db = {}; pa2 = STARTING; pb2 = STARTING
    for t in ta:
        day = datetime.fromtimestamp(t["ts"],tz=timezone.utc).strftime("%m-%d")
        da[day] = da.get(day,0) + (t["bank"]-pa2); pa2=t["bank"]
    for t in tb:
        day = datetime.fromtimestamp(t["ts"],tz=timezone.utc).strftime("%m-%d")
        db[day] = db.get(day,0) + (t["bank"]-pb2); pb2=t["bank"]
    for day in sorted(set(list(da.keys())+list(db.keys()))):
        a=da.get(day,0); b=db.get(day,0)
        print(f"  {day}: A=${a:>+9.2f}  B=${b:>+9.2f}  {'🟢B' if b>a else '🔴A' if a>b else '⚪'}")
    
    output = {"a":{"trades":len(ta),"wins":wa,"wr":round(wa/len(ta)*100,1) if ta else 0,"pnl":round(pa,2),"final":round(fa,2),"max_dd":round(mda*100,1)},
              "b":{"trades":len(tb),"wins":wb,"wr":round(wb/len(tb)*100,1) if tb else 0,"pnl":round(pb,2),"final":round(fb,2),"max_dd":round(mdb*100,1),"boosts":boosts},
              "days":DAYS}
    with open("/root/.openclaw/workspace-novakash/novakash/backtest_5min_comparison.json","w") as f:
        json.dump(output,f,indent=2)
    print(f"\n📁 Saved: backtest_5min_comparison.json")
    print(f"{'='*70}")

asyncio.run(main())
