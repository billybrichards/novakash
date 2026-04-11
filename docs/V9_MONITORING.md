# v9.0 Monitoring Guide

**Deploy date:** TBD
**Montreal server:** `15.223.247.178`

---

## Telegram Notifications to Watch

### 1. Source Disagreement Skip (NEW in v9.0)
```
🔀 SOURCE DISAGREE — BTC 5m
━━━━━━━━━━━━━━━━━━━━━━
Chainlink: DOWN (Δ -0.0475%)
Tiingo: UP (Δ +0.0073%)
VPIN: 0.620 | TRANSITION
Offset: T-70
Action: ⏭ SKIP (9.1% WR when disagree)

📍 MTL  BTC-1775560200  v9.0
```

**What it means:** CL and TI pointed different directions. v9.0 skipped the trade.
**What to check after window resolves:** Did the oracle go with CL or TI?
- In our data: CL was right 80.6% of the time when they disagreed
- These skips save us from the 9.1% WR disaster (engine signal + disagree)
- **If you see many consecutive disagrees:** market may be choppy/ranging — good time to stay out

### 2. Trade Placed (existing, now with v9 context)
```
✅ WIN — BTC 5m | v9.0
━━━━━━━━━━━━━━━━━━━━━━
Bet: DOWN @ $0.650 → Oracle: DOWN ✓
P&L: +$2.10
Sources: CL=DOWN TI=DOWN (AGREE ✓)
Tier: GOLDEN | Cap: $0.65 + π
Type: FAK | Filled: 15.38 shares
```

### 3. FAK Partial Fill (NEW)
If FAK fills less than requested, you'll see `partial: true` in the logs.
This means CLOB had some liquidity at our price but not enough for the full position.
**This is fine** — we got what exists, no risk of bad fills.

---

## Key Metrics to Monitor

### Win Rate by Source Agreement
```sql
-- On Railway DB:
SELECT
  CASE WHEN tiingo_direction = chainlink_direction THEN 'AGREE' ELSE 'DISAGREE' END as agreement,
  count(*) as n,
  count(*) FILTER (WHERE oracle_winner = chainlink_direction) as cl_wins,
  round(count(*) FILTER (WHERE oracle_winner = chainlink_direction)::numeric / count(*) * 100, 1) as cl_wr
FROM window_predictions
WHERE oracle_winner IS NOT NULL AND asset='BTC'
  AND chainlink_direction IS NOT NULL AND tiingo_direction IS NOT NULL
GROUP BY 1;
```

**Expected:** AGREE ~94% WR, DISAGREE: CL correct ~80% (but our signal would be wrong)

### v9.0 Gate Pass Rate
```sql
-- How many windows pass all v9 gates?
SELECT
  count(*) as total_evals,
  count(*) FILTER (WHERE decision = 'TRADE') as trades,
  count(*) FILTER (WHERE gate_failed = 'source_disagree') as disagree_skips,
  count(*) FILTER (WHERE gate_failed LIKE '%VPIN%') as vpin_skips
FROM signal_evaluations
WHERE asset='BTC' AND evaluated_at > now() - interval '24 hours';
```

### Fill Rate by Order Type
```sql
SELECT execution_mode, outcome,
  count(*), round(avg(CAST(entry_price AS numeric)), 3) as avg_entry
FROM trades
WHERE is_live = true AND created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 1, 2;
```

---

## Montreal Server Commands

```bash
# Check engine status
tail -100 /home/novakash/engine.log | grep -E 'v9\.|price_ladder|source_agree|source_disagree'

# Watch v9.0 decisions in real time
tail -f /home/novakash/engine.log | grep -E 'v9\.(source|cap_tier)|price_ladder\.(start|filled|exhausted)'

# Check source agreement rate
tail -500 /home/novakash/engine.log | grep 'v9.source' | awk '{print $NF}' | sort | uniq -c

# Check FAK fill results
tail -200 /home/novakash/engine.log | grep 'price_ladder.result'

# Check disagreement skips
tail -200 /home/novakash/engine.log | grep 'source_disagree'

# Quick WR check (last 50 resolved)
tail -1000 /home/novakash/engine.log | grep 'trade.resolved' | tail -50
```

---

## Execution HQ Dashboard

**URL:** `http://99.79.41.246/execution-hq` (AWS frontend)

### What to look at:
1. **Header WR badge** — rolling v9.0 win rate since deploy
2. **Source Agreement column** — green ✓ / red ✗ on each window
3. **Gate Pipeline** — 5-step visual: Agreement → VPIN → Delta → CG → Cap
4. **Tier column** — EARLY (cascade, $0.55) vs GOLDEN (T-130+, $0.65)
5. **Type column** — FAK/FOK/GTC per trade
6. **Fill price vs Cap** — are we getting fills at or below cap?

### Red flags:
- Source agreement rate drops below 60% → market is choppy, expect fewer trades
- Multiple consecutive DISAGREE skips → verify CL and TI feeds are both fresh
- FAK fills at cap+π but oracle goes against us → pi bonus may be too generous
- VPIN stuck below 0.45 for hours → NORMAL regime, few golden zone trades

---

## Rollback Procedure

If v9.0 is losing money or behaving unexpectedly:

```bash
# On Railway dashboard, change env vars:
V9_SOURCE_AGREEMENT=false
V9_CAPS_ENABLED=false
ORDER_TYPE=FOK
FIVE_MIN_EVAL_OFFSETS=240,180,120,60

# Then restart engine on Montreal:
ssh root@15.223.247.178
cd /home/novakash/novakash/engine
pkill -9 python3
nohup python3 main.py > /home/novakash/engine.log 2>&1 &
```

This restores exact v8.1.2 behavior. No code changes needed — all feature-flagged.

---

## First 24 Hours Checklist

- [ ] Verify source agreement notifications appear in Telegram
- [ ] Confirm FAK orders are submitting (check `price_ladder.start` in logs)
- [ ] Verify FAK fills (check `price_ladder.filled` — should see `order_type=FAK`)
- [ ] Check that DISAGREE windows are being skipped (not traded)
- [ ] Verify golden zone cap is $0.65 (check `v9.cap_tier` log)
- [ ] Check Execution HQ shows real data (not mock)
- [ ] After 10+ resolved trades: check WR matches expected ~90%+
- [ ] If WR < 70% after 20 trades: investigate or rollback
