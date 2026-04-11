# Trade Investigation Guide — Quick Reference

**For agents and humans investigating specific trades, losses, or system behavior.**

---

## 1. One-Query Full Picture (Today)

```sql
-- THE ONE QUERY: today's complete trading picture
SELECT
    tb.trade_id,
    tb.trade_outcome as result,
    round(tb.pnl_usd::numeric, 2) as pnl,
    tb.direction,
    tb.entry_reason,
    to_char(tb.placed_at, 'HH24:MI') as placed,
    to_char(tb.resolved_at, 'HH24:MI') as resolved,
    round(tb.entry_price::numeric, 2) as price,
    round(tb.stake_usd::numeric, 2) as stake,
    se.eval_offset as offset,
    se.regime,
    round(se.vpin::numeric, 3) as vpin,
    round(se.v2_probability_up::numeric, 3) as p_up,
    round(CASE WHEN tb.direction = 'YES' THEN se.v2_probability_up
               ELSE 1.0 - se.v2_probability_up END::numeric, 3) as dune_p,
    round(ws.cg_taker_buy_usd / NULLIF(ws.cg_taker_buy_usd + ws.cg_taker_sell_usd, 0) * 100, 1) as taker_buy_pct,
    round(ws.cg_top_long_pct::numeric, 1) as smart_long,
    round(ws.cg_funding_rate::numeric * 3 * 365, 1) as funding_annual,
    ws.twap_direction
FROM trade_bible tb
LEFT JOIN trades t ON t.id = tb.trade_id
LEFT JOIN LATERAL (
    SELECT * FROM signal_evaluations se2
    WHERE se2.window_ts = CAST(t.metadata->>'window_ts' AS bigint)
      AND se2.decision = 'TRADE'
    ORDER BY se2.evaluated_at DESC LIMIT 1
) se ON true
LEFT JOIN LATERAL (
    SELECT * FROM window_snapshots ws2
    WHERE ws2.window_ts = CAST(t.metadata->>'window_ts' AS bigint)
      AND ws2.asset = 'BTC'
    ORDER BY ws2.id DESC LIMIT 1
) ws ON true
WHERE tb.is_live = true AND tb.resolved_at > DATE_TRUNC('day', NOW())
ORDER BY tb.resolved_at DESC;
```

---

## 2. Investigate a Specific Loss

When you see a loss notification, use the cost/shares/price to find it:

```sql
-- Find by approximate cost and time
SELECT tb.trade_id, tb.trade_outcome, round(tb.pnl_usd::numeric,2) as pnl,
       tb.entry_reason, tb.direction,
       to_char(tb.resolved_at, 'HH24:MI:SS') as resolved,
       t.metadata->>'window_ts' as window_ts
FROM trade_bible tb
JOIN trades t ON t.id = tb.trade_id
WHERE tb.is_live = true
  AND tb.resolved_at > NOW() - INTERVAL '30 minutes'
ORDER BY tb.resolved_at DESC LIMIT 5;
```

Then get the FULL context for that window:

```sql
-- Replace WINDOW_TS with the value from above
-- Signal evaluations: every 2s eval for that window
SELECT eval_offset, v2_probability_up, regime, decision, gate_failed, vpin,
       delta_chainlink, delta_tiingo, evaluated_at
FROM signal_evaluations
WHERE window_ts = WINDOW_TS
ORDER BY evaluated_at;

-- Window snapshot: CG data, TWAP, Gamma at trade time
SELECT vpin, regime, delta_pct, direction, trade_placed,
       cg_taker_buy_usd, cg_taker_sell_usd, cg_top_long_pct, cg_top_short_pct,
       cg_funding_rate, cg_oi_delta_pct,
       twap_direction, twap_delta_pct, twap_agreement_score,
       gamma_up_price, gamma_down_price,
       v2_probability_up, v2_direction, skip_reason
FROM window_snapshots
WHERE window_ts = WINDOW_TS AND asset = 'BTC'
ORDER BY id DESC LIMIT 1;

-- Gate audit: what each gate decided
SELECT direction, regime, vpin, delta_pct, gate_passed, decision,
       gate_failed, skip_reason, v2_probability_up, v2_direction,
       eval_offset, oracle_outcome, would_have_won
FROM gate_audit
WHERE window_ts = WINDOW_TS
ORDER BY evaluated_at DESC LIMIT 1;
```

---

## 3. Performance Dashboards

### Today's summary
```sql
SELECT count(*) FILTER (WHERE trade_outcome='WIN') as W,
       count(*) FILTER (WHERE trade_outcome='LOSS') as L,
       round(count(*) FILTER (WHERE trade_outcome='WIN')::numeric / NULLIF(count(*),0)*100, 1) as wr,
       round(sum(pnl_usd)::numeric, 2) as pnl
FROM trade_bible WHERE is_live = true AND resolved_at > DATE_TRUNC('day', NOW());
```

### By regime
```sql
SELECT
    CASE WHEN entry_reason LIKE '%TRANSITION%' THEN 'TRANSITION'
         WHEN entry_reason LIKE '%CASCADE%' THEN 'CASCADE'
         WHEN entry_reason LIKE '%NORMAL%' THEN 'NORMAL'
         ELSE 'OTHER' END as regime,
    count(*) FILTER (WHERE trade_outcome='WIN') as W,
    count(*) FILTER (WHERE trade_outcome='LOSS') as L,
    round(sum(pnl_usd)::numeric, 2) as pnl
FROM trade_bible WHERE is_live = true AND resolved_at > DATE_TRUNC('day', NOW())
GROUP BY 1 ORDER BY 1;
```

### CG alignment vs outcome
```sql
SELECT
    CASE WHEN ws.cg_taker_buy_usd / NULLIF(ws.cg_taker_buy_usd + ws.cg_taker_sell_usd, 0) > 0.55
              AND t.direction = 'YES' THEN 'taker_aligned'
         WHEN ws.cg_taker_sell_usd / NULLIF(ws.cg_taker_buy_usd + ws.cg_taker_sell_usd, 0) > 0.55
              AND t.direction = 'NO' THEN 'taker_aligned'
         ELSE 'taker_not_aligned' END as taker,
    tb.trade_outcome, count(*), round(sum(tb.pnl_usd)::numeric, 2) as pnl
FROM trade_bible tb
JOIN trades t ON t.id = tb.trade_id
LEFT JOIN window_snapshots ws ON ws.window_ts = CAST(t.metadata->>'window_ts' AS bigint) AND ws.asset = 'BTC'
WHERE tb.is_live = true AND tb.resolved_at > DATE_TRUNC('day', NOW())
GROUP BY 1, 2 ORDER BY 1, 2;
```

### What-if threshold simulation
```sql
WITH evals AS (
    SELECT DISTINCT ON (window_ts) window_ts, eval_offset, v2_probability_up, regime,
        CASE WHEN delta_chainlink > 0 AND delta_tiingo > 0 THEN v2_probability_up
             WHEN delta_chainlink < 0 AND delta_tiingo < 0 THEN 1.0 - v2_probability_up
             ELSE NULL END as dune_p
    FROM signal_evaluations
    WHERE evaluated_at > DATE_TRUNC('day', NOW()) AND v2_probability_up IS NOT NULL AND eval_offset <= 180
    ORDER BY window_ts,
        CASE WHEN delta_chainlink > 0 AND delta_tiingo > 0 THEN v2_probability_up
             WHEN delta_chainlink < 0 AND delta_tiingo < 0 THEN 1.0 - v2_probability_up
             ELSE NULL END DESC NULLS LAST
),
with_outcomes AS (
    SELECT e.*, tb.trade_outcome FROM evals e
    LEFT JOIN trades t ON t.metadata->>'window_ts' = e.window_ts::text AND t.is_live = true
    LEFT JOIN trade_bible tb ON tb.trade_id = t.id
)
SELECT threshold,
    count(*) FILTER (WHERE dune_p >= threshold) as trades,
    count(*) FILTER (WHERE dune_p >= threshold AND trade_outcome='WIN') as W,
    count(*) FILTER (WHERE dune_p >= threshold AND trade_outcome='LOSS') as L,
    round(count(*) FILTER (WHERE dune_p >= threshold AND trade_outcome='WIN')::numeric /
          NULLIF(count(*) FILTER (WHERE dune_p >= threshold AND trade_outcome IS NOT NULL),0)*100, 1) as wr
FROM with_outcomes, (VALUES (0.60),(0.65),(0.70),(0.75),(0.80),(0.85)) AS t(threshold)
GROUP BY threshold ORDER BY threshold;
```

---

## 4. Data Sources (Source of Truth Hierarchy)

| Table | What | Completeness | Use for |
|-------|------|-------------|---------|
| **trade_bible** | Resolved trades with outcome | **SOURCE OF TRUTH** for W/L/PnL | Performance reporting, SITREP |
| **trades** | All placed trades | Synced from trade_bible every 60s | Order lifecycle, metadata |
| **signal_evaluations** | Every 2s eval decision | Has dune_p even for SKIPs | Threshold analysis, what-if |
| **window_snapshots** | Per-window CG/TWAP/Gamma | v10.3: includes TWAP for TRADE path | Feature analysis, CG correlation |
| **gate_audit** | Per-eval gate decisions | Has direction + would_have_won (backfilled) | ML training, gate optimization |
| **wallet_snapshots** | Balance over time | Sampled ~1/min | Bankroll tracking |

### Auto-sync mechanisms
- **trade_bible**: Auto-populated by PostgreSQL trigger when trades.outcome is set
- **trades → trade_bible sync**: Reconciler `_sync_bible_to_trades()` runs every 60s
- **Orphan detection**: Reconciler `_resolve_orphaned_fills()` runs every 60s, checks CLOB trade history
- **SITREP**: Reads from trade_bible (not trades table)

---

## 5. Montreal Engine Access

```bash
# Generate temp key and SSH (60s window)
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b --region ca-central-1
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key novakash@15.223.247.178

# Engine log
tail -50 /home/novakash/engine.log

# Grep for specific events
grep -a 'v10.trade\|LOSS\|reconciler.*resolved' /home/novakash/engine.log | tail -10
```

### DB access (from anywhere)
```bash
PGPASSWORD=wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway
```
