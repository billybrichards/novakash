# Architecture Improvements — Data Analysis & Observability

**Date:** 2026-04-14  
**Context:** Issues discovered during go-live debugging session

---

## Priority 1: Signal Context Persistence (Missing)

### Problem
When a strategy makes a decision (TRADE or SKIP), we don't save the full signal context that led to that decision. The `strategy_decisions` table has `metadata_json` but it's inconsistently populated. We can't reconstruct WHY a specific trade was placed without grepping engine logs.

### Fix
Add a `decision_context` JSONB column to `strategy_decisions` (or a dedicated `strategy_decision_context` table) that saves:
- All gate results (passed/failed, reason, data)
- Full data surface snapshot (v2_probability_up, poly_direction, poly_confidence_distance, vpin, regime, delta sources)
- CLOB state at decision time (up_ask, down_ask, spread, mid)
- CoinGlass snapshot (OI, funding, taker ratio, L/S ratio)
- Eval offset and window_ts
- The specific confidence_distance that was used

This is the single most important missing piece for post-hoc analysis.

---

## Priority 2: System Event Log Table (Missing)

### Problem
No `system_events` table. Mode switches, deploys, config changes, kill switch activations, and error conditions are only in engine.log which gets rotated on every deploy. Critical operational history is lost.

### Fix
Create `system_events` table:
```sql
CREATE TABLE system_events (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,  -- mode_switch, deploy, config_change, kill_switch, error
    severity VARCHAR(10) NOT NULL,    -- info, warning, error, critical
    message TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_system_events_type ON system_events(event_type, created_at);
```

Write to this on: mode switches, deploy completions, config syncs, kill switch triggers, consecutive loss cooldowns, feed disconnects, TimesFM outages.

---

## Priority 3: Trade Lifecycle Audit Trail (Incomplete)

### Problem
Trades go OPEN → (silence) → RESOLVED. We don't track:
- When the CLOB order was actually matched (vs placed)
- Polymarket resolution timestamp
- How many eval ticks passed before a trade was placed
- Which gate was the last to pass (the "deciding" gate)

### Fix
Add `trade_events` table:
```sql
CREATE TABLE trade_events (
    id BIGSERIAL PRIMARY KEY,
    trade_id INTEGER REFERENCES trades(id),
    event_type VARCHAR(30),  -- placed, matched, partially_filled, resolved, expired, redeemed
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Priority 4: Data Surface Snapshots (Missing for Replay)

### Problem
`signal_evaluations` captures some signal state but not the full `FullDataSurface` object. Can't replay the exact inputs a gate saw. The `window_snapshots` table has grown to 130+ columns trying to capture everything — it's become a god-table.

### Fix
Two options:
1. **Dedicated `data_surface_snapshots` table** with a single JSONB column that stores the serialized `FullDataSurface` per eval_offset per window. ~500 bytes per snapshot, ~100K rows/day.
2. **Or** save to S3/file per window (cheaper for storage, harder to query).

Option 1 is better for SQL analysis.

---

## Priority 5: Proper Log Aggregation (Missing)

### Problem
Engine logs are on Montreal's filesystem, rotated on deploy, not searchable. Debugging requires SSH + grep. Telegram alerts are the primary observability tool — fragile.

### Fix Options
1. **Lightweight:** Write structured logs to a `log_entries` table with severity/component/message. Query via Hub API.
2. **Medium:** Ship logs to CloudWatch (already on AWS) via awslogs driver.
3. **Heavy:** Loki/Grafana stack (overkill for current scale).

Recommendation: Option 1. A `log_entries` table with TTL (delete > 7 days) that the Hub can query. Add a `/api/logs` endpoint.

---

## Priority 6: Window Outcome Labeling Delay

### Problem
`window_snapshots.actual_direction` is populated by the `label_resolved_windows()` pass in the reconcile loop. This happens every 2 minutes. But the window closes at T=0, and the actual direction is knowable immediately from Chainlink/Tiingo price at close. There's a 2-minute minimum delay before we can analyze outcomes.

### Fix
Label windows immediately when the window closes (in the 5-min feed callback), not in the reconcile loop. The actual_direction = `close_price > open_price → UP` — computable instantly.

---

## Priority 7: Strategy Decision Dedup Architecture

### Problem
`window_states` table for dedup was missing from schema (had to create manually). The `mark_traded` call happens AFTER the order is placed — if it fails, the next eval tick places another order. Three duplicate orders placed on 2026-04-14 09:03 due to this.

### Fix
1. `mark_traded` should happen BEFORE order placement (optimistic lock)
2. Use DB-level unique constraint: `UNIQUE(strategy, window_ts)` on `window_states`
3. If mark_traded fails → don't place the order
4. Auto-create table in Alembic migration, not ad-hoc

---

## Priority 8: Config Source of Truth

### Problem
Three config sources compete: `.env` file, DB `trading_configs`, and YAML strategy configs. During this session:
- `.env` had `PAPER_MODE=true` that reset live mode on every deploy
- DB had `bankroll=130.82` (stale) while wallet was $57
- YAML had `fraction: 0.025` overriding `runtime.bet_fraction: 0.07`

### Fix
1. `.env` should only contain secrets and service URLs — never operational state
2. DB `trading_configs` is the source of truth for all risk/sizing params
3. YAML strategy configs define structure (which gates, which direction) — never sizing params
4. Sizing params should ALWAYS come from runtime config (synced from DB)

---

## Not Urgent But Valuable

- **Backtest framework** using `signal_evaluations` + `window_snapshots` — replay strategy decisions against historical data
- **Strategy performance dashboard** in Hub UI — live WR, PnL curve, gate rejection rates
- **Alert dedup** — the POLY-SOT divergence alerts fire for every diverged trade on every 2-min loop, flooding Telegram
- **TimesFM health monitoring** — auto-restart on OOM or connection reset pattern
