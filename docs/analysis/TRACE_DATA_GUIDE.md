# Trace data — which table for what

**Last updated:** 2026-04-16 after surface-persistence fix

## TL;DR

| I want… | Use |
|---|---|
| Which strategy TRADED vs SKIPPED a window + why | `strategy_decisions` |
| Which gates passed / failed for a strategy on a window | `gate_check_traces` |
| Full v3/v4 surface data at evaluation time (JSONB) | `window_evaluation_traces.surface_json` |
| Fast SQL filter on v3/v4 columns (no JSONB extract) | `window_snapshots` (after v4.4.0) |
| Wallet balance over time (CLOB-verified) | `wallet_snapshots` |
| Trade outcome + PnL + strategy attribution | `trades` (⚠️ see warning) |
| Per-window oracle resolution (UP/DOWN) | `window_snapshots.poly_winner` or `window_predictions.oracle_winner` |
| Source-of-truth P&L | `wallet_snapshots` deltas minus deposits — NOT the trades table |

## Tables in detail

### `wallet_snapshots`
Every 2s CLOB balance poll. **Authoritative** for wallet P&L. Rows include the deposit bump you saw at 20:28 UTC Apr 15 → $114.68. Memory note `reference_clob_audit.md` says: "The DB lies — always check the CLOB". This table IS the CLOB view.

**Columns:** `recorded_at, balance_usdc, source` (`clob_reconciler` for the main poll).
**Cadence:** ~2s
**Rows:** ~43k/day

### `window_evaluation_traces`
One row per strategy evaluation (TRADE or SKIP). JSONB blob of the FULL `FullDataSurface` — all 77 fields including v3 sub-signals, v4 regime, v4 conviction, v4 macro, poly direction, chainlink/tiingo/binance deltas.

**Use when:** you need surface values at decision time for ML / analysis.
**Watch out:** JSONB extraction is slow. For repeated filters, use denormalized columns in `window_snapshots`.

```sql
-- Example: average v3 vpin signal for winning v4_fusion trades last 24h
SELECT AVG((surface_json->>'v3_sub_vpin')::float)
FROM window_evaluation_traces t
JOIN trades tr ON tr.window_ts = t.window_ts AND tr.asset = t.asset
WHERE tr.strategy = 'v4_fusion'
  AND tr.outcome = 'WIN'
  AND t.assembled_at > NOW() - INTERVAL '24 hours';
```

### `gate_check_traces`
One row per gate per strategy per window. Captures gate pass/fail with a JSONB `observed_json` of the values the gate looked at.

**Use when:** debugging why a specific gate blocked a specific trade.

```sql
-- Which gates have been firing most on v4_fusion this hour
SELECT gate_name, passed, COUNT(*)
FROM gate_check_traces
WHERE strategy_id = 'v4_fusion'
  AND evaluated_at > NOW() - INTERVAL '1 hour'
GROUP BY gate_name, passed
ORDER BY COUNT(*) DESC;
```

### `strategy_decisions`
One row per strategy evaluation outcome. TRADE vs SKIP + `skip_reason`.

**Use when:** aggregating which strategies traded and why others skipped.

```sql
-- Skip-reason breakdown for v4_fusion last 6h
SELECT skip_reason, COUNT(*)
FROM strategy_decisions
WHERE strategy_id = 'v4_fusion' AND action = 'SKIP'
  AND created_at > NOW() - INTERVAL '6 hours'
GROUP BY skip_reason ORDER BY 2 DESC;
```

### `window_snapshots`
Legacy denormalized table. After v4.4.0 (2026-04-16), populates 17 new v3/v4 columns via the StrategyRegistry → `db.update_window_surface_fields` path.

**Use when:** you want fast SQL without JSONB extraction.

```sql
-- v4_fusion trades where regime_confidence >= 0.9 AND sub_signal_vpin > 0.5
SELECT tr.*, ws.strategy_conviction, ws.sub_signal_vpin, ws.regime_confidence
FROM trades tr
JOIN window_snapshots ws USING (window_ts, asset, timeframe)
WHERE tr.strategy = 'v4_fusion'
  AND ws.regime_confidence >= 0.9
  AND ws.sub_signal_vpin > 0.5
  AND tr.created_at > NOW() - INTERVAL '48 hours';
```

### `trades` — ⚠️ use cautiously
Per memory note `reference_clob_audit.md`:

> **The DB lies — always check the CLOB.**
> `trades` table:
> - `EXPIRED` = GTC order never filled, stake returned (NOT a loss)
> - `OPEN` with large stake = may have resolved on-chain already
> - `RESOLVED_LOSS` only set if reconciler caught it — has known missed-resolution bugs
> - Stakes in DB can be 10-50× what actually executed
>
> Never report wallet P&L from DB alone. Always verify on-chain.

For trusted P&L use `wallet_snapshots` deltas + Polymarket `activity` API. For strategy attribution IS still the only source — just handle outcome buckets carefully.

### `window_snapshots.poly_winner` / `window_predictions.oracle_winner`
Oracle-resolved direction (UP/DOWN) for a window. `poly_winner` is from the Polymarket resolution (delayed ~30s after close). `oracle_winner` in `window_predictions` is backfilled from gamma API.

**After v4.4.0**, `window_states.actual_direction` also populated by reconciler (see PR #213).

## Common joins

### "What was the v4 regime when trade X fired?"
```sql
SELECT t.*, w.strategy_conviction, w.regime_confidence,
       (wet.surface_json->>'v4_regime') AS regime
FROM trades t
JOIN window_snapshots w USING (window_ts, asset, timeframe)
JOIN window_evaluation_traces wet
  ON wet.window_ts = t.window_ts
 AND wet.asset = t.asset
 AND wet.timeframe = t.timeframe
 AND wet.eval_offset = w.eval_offset
WHERE t.id = 4731;
```

### "Which gates blocked v4_fusion on the 08:00 UTC 5m window today?"
```sql
SELECT gate_order, gate_name, passed, skip_reason, observed_json
FROM gate_check_traces
WHERE strategy_id = 'v4_fusion'
  AND asset = 'BTC'
  AND timeframe = '5m'
  AND window_ts = EXTRACT(EPOCH FROM '2026-04-16 08:00'::timestamptz)::bigint
ORDER BY eval_offset DESC, gate_order ASC;
```

## Scripts

- `scripts/ops/strategy_pnl_24h.py` — 24h per-strategy P&L from `trades` table (⚠️ biased, see warning above)
- TODO audit #191 — wallet-truth P&L script using `wallet_snapshots` + Polymarket activity API

## Populated-fields summary (as of 2026-04-16 10:40 UTC)

| Table | Rows/24h | % fields populated |
|---|---|---|
| `wallet_snapshots` | 43,200 | 100% |
| `window_evaluation_traces` | 42,306 | 63/77 (82%) |
| `gate_check_traces` | 477,987 | 100% |
| `window_snapshots` (before v4.4.0) | 21,270 | 68/151 (45%) |
| `window_snapshots` (after v4.4.0, expected) | 21,270 | 85/151 (56%) |
| `strategy_decisions` | ~200,000 | 100% |
| `trades` | 150-300 | 95% (strategy_id NULL pre-PR #211) |
