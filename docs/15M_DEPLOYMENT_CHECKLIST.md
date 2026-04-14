# 15m Strategy Deployment Checklist

## Pre-Deployment

- [ ] PR #172 merged to `develop`
- [ ] TimesFM service updated with CLOB 15m support (novakash-timesfm-repo commit d220c26)
- [ ] Environment variables set:
  ```bash
  FIFTEEN_MIN_ENABLED=true
  FIFTEEN_MIN_ASSETS=BTC
  ENGINE_USE_STRATEGY_REGISTRY=true
  PAPER_MODE=true
  ```

## Deploy Steps

1. **Deploy TimesFM service** (novakash-timesfm-repo)
   ```bash
   git pull origin main
   # Restart service to pick up CLOB 15m support
   ```

2. **Deploy Engine + Hub** (novakash)
   ```bash
   git checkout develop
   git pull origin develop
   docker-compose up -d --build
   ```

3. **Create monitoring task**
   ```bash
   psql $DATABASE_URL -f scripts/create_15m_monitoring_task.sql
   ```

4. **Verify services**
   - Hub: `curl http://localhost:8000/health`
   - Engine logs: `docker logs btc-trader-engine-1 -f | grep 15m`
   - Frontend: http://localhost:3000/polymarket/15min

## Verification (First 24 Hours)

### Data Flow Checks

- [ ] **15m windows detected**
  ```sql
  SELECT COUNT(*) FROM strategy_decisions WHERE timeframe='15m';
  -- Expected: ~96 per day
  ```

- [ ] **All strategies evaluating**
  ```sql
  SELECT strategy_id, COUNT(*) 
  FROM strategy_decisions 
  WHERE timeframe='15m' 
  GROUP BY strategy_id;
  -- Expected: v15m_down_only, v15m_up_asian, v15m_up_basic, v15m_fusion, v15m_gate
  ```

- [ ] **CLOB data present**
  ```sql
  SELECT COUNT(*) FROM ticks_clob WHERE timeframe='15m';
  SELECT COUNT(*) FROM clob_book_snapshots WHERE timeframe='15m';
  -- Expected: Non-zero, growing every 2s
  ```

- [ ] **V4 snapshot includes 15m**
  ```bash
  curl "http://localhost:8001/v4/snapshot?timescales=5m,15m&asset=BTC" | jq '.timescales | keys'
  # Expected: ["15m", "5m"]
  ```

- [ ] **Window snapshots persist**
  ```sql
  SELECT COUNT(*) FROM window_snapshots WHERE timeframe='15m';
  -- Expected: Non-zero after first 15m window resolves
  ```

### Telegram Alert Checks

- [ ] 15m window CLOSING alerts appear
- [ ] Strategy decision summaries show all 5 strategies
- [ ] No error/crash alerts for 15m evaluation
- [ ] Window resolution alerts show `timeframe: 15m`
- [ ] CLOB data referenced in alerts (if late_window trades occur)

### Frontend Monitor

- [ ] `/polymarket/15min` page loads without errors
- [ ] Config viewer shows all 5 strategy YAMLs
- [ ] Window comparison table populates as windows close
- [ ] P&L boxes calculate correctly
- [ ] Data auto-refreshes every 10s

### Engine Logs

```bash
docker logs btc-trader-engine-1 -f | grep -i "15m\|fifteen"
```

**Expected log patterns:**
- `fifteen_min.window_opened`
- `fifteen_min.CLOSING_signal`
- `strategy_registry_v2.decision_15m` (5 times per window at CLOSING)
- No `fifteen_min.registry_eval_error`

## Success Criteria (7 Days)

After 168 hours (7 days) of paper mode:

- [ ] **Minimum windows:** 50 unique 15m windows evaluated
- [ ] **All strategies active:** Each strategy has > 20 decisions
- [ ] **CLOB availability:** > 80% of windows have CLOB data
- [ ] **No crashes:** Zero strategy evaluation errors
- [ ] **Reconciliation works:** Paper trades resolve correctly
- [ ] **Data quality:** window_snapshots, strategy_decisions, ticks_clob all populated
- [ ] **Telegram alerts:** > 95% of windows trigger alerts

## Queries for Analysis

### Strategy Win Rates (Hypothetical)
```sql
SELECT 
  strategy_id,
  COUNT(*) FILTER (WHERE action='TRADE') AS trades,
  COUNT(*) FILTER (WHERE action='SKIP') AS skips,
  ROUND(COUNT(*) FILTER (WHERE action='TRADE')::numeric / 
        NULLIF(COUNT(*), 0) * 100, 1) AS trade_pct
FROM strategy_decisions
WHERE timeframe='15m'
GROUP BY strategy_id
ORDER BY trade_pct DESC;
```

### Most Common Skip Reasons
```sql
SELECT 
  strategy_id,
  skip_reason,
  COUNT(*) AS cnt
FROM strategy_decisions
WHERE timeframe='15m' AND action='SKIP'
GROUP BY strategy_id, skip_reason
ORDER BY cnt DESC
LIMIT 20;
```

### CLOB Data Coverage
```sql
SELECT 
  DATE(evaluated_at) AS date,
  COUNT(*) AS total_decisions,
  COUNT(*) FILTER (WHERE metadata_json->>'clob_implied_up' IS NOT NULL) AS with_clob,
  ROUND(COUNT(*) FILTER (WHERE metadata_json->>'clob_implied_up' IS NOT NULL)::numeric / 
        NULLIF(COUNT(*), 0) * 100, 1) AS clob_coverage_pct
FROM strategy_decisions
WHERE timeframe='15m'
GROUP BY DATE(evaluated_at)
ORDER BY date DESC;
```

### Timing Distribution
```sql
SELECT 
  eval_offset,
  COUNT(*) AS decisions
FROM strategy_decisions
WHERE timeframe='15m' AND action='TRADE'
GROUP BY eval_offset
ORDER BY eval_offset DESC;
```

## Troubleshooting

### No 15m windows appearing
- Check `FIFTEEN_MIN_ENABLED=true` in engine env
- Check polymarket_5min.py feed is running
- Verify 15m markets exist on Polymarket

### Empty strategy_decisions table
- Check `ENGINE_USE_STRATEGY_REGISTRY=true`
- Verify registry loaded configs: `grep "v15m_" engine logs`
- Check timescale filter isn't blocking: `grep "timescale" engine logs`

### Missing CLOB data
- TimesFM service must be on latest commit (d220c26)
- Check CLOB feed is running: `grep "clob_feed" engine logs`
- Verify Polymarket CLOB endpoint is accessible (Montreal only)

### Telegram alerts not sending
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set
- Check telegram.py supports timeframe parameter (Line 1716)
- Look for `telegram.send_window_resolution` in logs

### Frontend monitor errors
- Hub API must be running on :8000
- Check `/api/strategy-decisions/15m` endpoint
- Verify `/api/strategy-configs/{id}` returns YAML

## Rollback Plan

If critical issues found:

1. **Disable 15m windows**
   ```bash
   docker exec btc-trader-engine-1 sh -c 'export FIFTEEN_MIN_ENABLED=false && supervisorctl restart engine'
   ```

2. **Keep data for analysis**
   - DO NOT drop strategy_decisions/ticks_clob data
   - Export for offline analysis:
     ```bash
     psql $DATABASE_URL -c "COPY (SELECT * FROM strategy_decisions WHERE timeframe='15m') TO STDOUT CSV HEADER" > 15m_decisions.csv
     ```

3. **Report issues**
   - GitHub issue with logs, queries, screenshots
   - Tag audit task as BLOCKED with reason

## Next Steps (After 7 Days)

If success criteria met:

- [ ] Backtest 15m strategies with collected data
- [ ] Tune confidence thresholds per strategy
- [ ] Enable 1-2 strategies in LIVE mode (low stake)
- [ ] Monitor live performance for 48 hours
- [ ] Scale up if profitable

## Related Files

- Design spec: `docs/superpowers/specs/2026-04-14-15m-clean-arch-design.md`
- Handover: `docs/HANDOVER_15M_CLEAN_ARCH.md`
- Monitor page: `frontend/src/pages/FifteenMinMonitor.jsx`
- Strategy configs: `engine/strategies/configs/v15m_*.yaml`
- Audit task SQL: `scripts/create_15m_monitoring_task.sql`
