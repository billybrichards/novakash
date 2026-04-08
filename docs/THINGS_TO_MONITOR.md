# Things to Monitor — v10.3 Live Trading

**Last updated:** 2026-04-08 21:00 UTC
**Engine version:** v10.3 (ELM v3 + CG taker gate + DOWN penalty)
**Config:** Full v10.3 — all features active except Kelly sizing

---

## 1. Irreducible Losses (Cost of Business)

Some losses pass ALL gates because the model was genuinely confident and CG data was neutral. These are not bugs — they're the natural variance of a 62%+ WR system.

**Apr 8 examples:**
- TRANSITION T-124 (-$3.99): dune_p=0.78, threshold=0.732, CG neutral → **PASSED all gates, still lost**
- TRANSITION T-180 (-$3.34): dune_p=0.78, threshold=0.760, CG neutral → **PASSED all gates, still lost**

**What to monitor:**
- If losses with "CG neutral" consistently lose → consider tightening CG confirmation penalty (currently +0.02 for 0 confirms)
- If losses cluster at specific dune_p ranges (e.g. 0.75-0.80) → the model may be overconfident in that band
- Track: `SELECT dune_p range, WR` from signal_evaluations weekly

**Acceptable loss rate:** At 62% WR, expect ~4 losses per 10 trades. If WR drops below 55% over 50+ trades, investigate.

```sql
-- Monitor: losses by CG confirmation count
SELECT
    CASE WHEN ws.cg_taker_buy_usd / NULLIF(ws.cg_taker_buy_usd + ws.cg_taker_sell_usd, 0) > 0.55 THEN 'taker_aligned'
         WHEN ws.cg_taker_sell_usd / NULLIF(ws.cg_taker_buy_usd + ws.cg_taker_sell_usd, 0) > 0.55 THEN 'taker_opposing'
         ELSE 'taker_neutral' END as taker_status,
    tb.trade_outcome, count(*), round(sum(tb.pnl_usd)::numeric, 2) as pnl
FROM trade_bible tb
JOIN trades t ON t.id = tb.trade_id
LEFT JOIN window_snapshots ws ON ws.window_ts = CAST(t.metadata->>'window_ts' AS bigint) AND ws.asset = 'BTC'
WHERE tb.is_live = true AND tb.resolved_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2 ORDER BY 1, 2;
```

---

## 2. CG Taker Gate Effectiveness

**Status:** ENABLED (`V10_CG_TAKER_GATE=true`)

The CG taker gate hard-blocks trades when taker flow (>55%) AND smart money (>52%) both oppose our direction. This is our strongest loss filter — 81.7% WR when aligned vs 58.3% when both opposing.

**What to monitor:**
- How many trades does it block per day? (Should be ~10-20% of attempts)
- Of the blocked trades, how many would have won? (check resolved windows)
- If it blocks too aggressively (>30% of attempts), consider raising `V10_CG_TAKER_OPPOSING_PCT` from 55 to 60

```sql
-- Monitor: gate block rate (from signal_evaluations)
SELECT gate_failed, count(*),
       round(count(*)::numeric / sum(count(*)) OVER () * 100, 1) as pct
FROM signal_evaluations
WHERE evaluated_at > NOW() - INTERVAL '24 hours' AND decision = 'SKIP'
GROUP BY gate_failed ORDER BY count(*) DESC;
```

---

## 3. DOWN Direction Penalty

**Status:** ACTIVE (`V10_DOWN_PENALTY=0.03`)

DOWN predictions are 9.3pp less accurate than UP (72.5% vs 81.8%, N=865). The +0.03 threshold penalty compensates.

**What to monitor:**
- DOWN vs UP WR in live trades — is the gap narrowing with the penalty?
- If DOWN WR drops below 65%, consider increasing penalty to 0.05
- If DOWN WR equals UP WR, the penalty is working perfectly

```sql
-- Monitor: UP vs DOWN WR
SELECT direction,
       count(*) FILTER (WHERE trade_outcome='WIN') as W,
       count(*) FILTER (WHERE trade_outcome='LOSS') as L,
       round(count(*) FILTER (WHERE trade_outcome='WIN')::numeric / count(*) * 100, 1) as wr
FROM trade_bible WHERE is_live = true AND resolved_at > NOW() - INTERVAL '24 hours'
GROUP BY direction;
```

---

## 4. Offset Zone Performance

**What to monitor:**
- T-60..120 should be highest WR (model most accurate close to window close)
- T-120..180 should be profitable but lower WR
- T>180 is BLOCKED by `V10_MIN_EVAL_OFFSET=180` — verify no trades slip through

```sql
-- Monitor: WR by offset zone
SELECT
    CASE WHEN se.eval_offset <= 80 THEN 'T-60..80'
         WHEN se.eval_offset <= 120 THEN 'T-80..120'
         WHEN se.eval_offset <= 180 THEN 'T-120..180'
         ELSE 'T-180+' END as zone,
    count(*) FILTER (WHERE tb.trade_outcome='WIN') as W,
    count(*) FILTER (WHERE tb.trade_outcome='LOSS') as L,
    round(sum(tb.pnl_usd)::numeric, 2) as pnl
FROM signal_evaluations se
JOIN trades t ON t.metadata->>'window_ts' = se.window_ts::text AND t.is_live = true
JOIN trade_bible tb ON tb.trade_id = t.id
WHERE se.decision = 'TRADE' AND se.evaluated_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1;
```

---

## 5. Regime Classification Accuracy

**Known issue:** Two conflating regime systems exist:
- **VPIN-based** (CALM/NORMAL/TRANSITION/CASCADE) — used by v10 gates
- **Volatility-based** (LOW_VOL/NORMAL/HIGH_VOL/TRENDING) — used by regime_classifier.py

The VPIN-based regime is what matters for trading decisions. TRANSITION (VPIN 0.55-0.65) has 85% WR — it's our best regime despite the misleading name.

**What to monitor:**
- TRANSITION WR should stay >75% — if it drops, VPIN thresholds may need adjustment
- CASCADE WR (VPIN >0.65) — historically mixed. Watch for deterioration.
- If VPIN is consistently >0.65 (CASCADE), market may be choppy — consider pausing

---

## 6. Fill Rate and Minimum Share Size

**Status:** Polymarket minimum = 5 shares. Engine enforces min 5 in fok_ladder.py and polymarket_client.py.

**What to monitor:**
- `ABSOLUTE_MAX_BET=10.0`, `BET_FRACTION=0.075` → at $50 bankroll = $3.75 stake = 5.36 shares at $0.70
- If bankroll drops below ~$47, stake at $0.70 drops below 5 shares → engine bumps to min 5 (higher risk per trade)
- Watch for `guardrail.circuit_breaker.4xx` errors in engine log — means Polymarket rejected an order

---

## 7. Reconciler Health

**What to monitor:**
- `reconciler.orphan_check` should fire every 60s with count of orphans found
- `reconciler.bible_sync` should fire every 60s, syncing trade_bible → trades table
- `clob_feed.write_error` is a known non-critical error (CLOB tick recorder schema mismatch) — noisy but harmless
- Orphan count should trend toward 0 over time

```sql
-- Monitor: unresolved orphans
SELECT count(*) as orphans FROM trades
WHERE is_live = true AND outcome IS NULL
  AND metadata->>'clob_status' IN ('MATCHED', 'RESTING');
```

---

## 8. ELM v3 Model Health

**Endpoint:** `GET http://3.98.114.0:8080/v2/probability?asset=BTC&seconds_to_close=N`
**Model:** `V10_DUNE_MODEL=oak` (ELM v3 production, NOT cedar/DUNE)

**What to monitor:**
- P(UP) distribution should be smooth [0.15-0.85], NOT bimodal (0.01 or 0.99 = wrong model)
- If model returns errors → engine passes through (no block), but trades are ungated
- Check model health: `curl http://3.98.114.0:8080/v2/health`

---

## 9. Wallet and Bankroll

**Starting:** $131 (Apr 8)
**Current:** ~$55-65 (after losses + recovered orphans)

**What to monitor:**
- Daily loss limit: `DAILY_LOSS_LIMIT_PCT=0.30` (30% = ~$18 at current bankroll)
- Consecutive loss cooldown: `CONSECUTIVE_LOSS_COOLDOWN=10` (10 losses before pause)
- If wallet drops below $35, BET_FRACTION=7.5% gives only $2.63 stake → min 5 shares forces $3.50+ per trade → risk concentration increases

---

## 10. SITREP Accuracy

**Fixed Apr 8:** SITREP now reads from `trade_bible` (source of truth) instead of `trades` table.

**What to monitor:**
- SITREP W/L count should match `SELECT count(*) FROM trade_bible WHERE is_live=true AND resolved_at > DATE_TRUNC('day', NOW())`
- If mismatch returns → the `_sync_bible_to_trades()` method may be failing
- Recent wins/losses in SITREP should show entries from trade_bible with correct entry_reason

---

## Quick Health Check SQL

```sql
-- One-shot health check: run this to see everything
SELECT 'today' as period,
    count(*) FILTER (WHERE trade_outcome='WIN') as W,
    count(*) FILTER (WHERE trade_outcome='LOSS') as L,
    round(count(*) FILTER (WHERE trade_outcome='WIN')::numeric / NULLIF(count(*),0)*100, 1) as wr,
    round(sum(pnl_usd)::numeric, 2) as pnl
FROM trade_bible WHERE is_live = true AND resolved_at > DATE_TRUNC('day', NOW())
UNION ALL
SELECT 'last_hour',
    count(*) FILTER (WHERE trade_outcome='WIN'),
    count(*) FILTER (WHERE trade_outcome='LOSS'),
    round(count(*) FILTER (WHERE trade_outcome='WIN')::numeric / NULLIF(count(*),0)*100, 1),
    round(sum(pnl_usd)::numeric, 2)
FROM trade_bible WHERE is_live = true AND resolved_at > NOW() - INTERVAL '1 hour';
```
