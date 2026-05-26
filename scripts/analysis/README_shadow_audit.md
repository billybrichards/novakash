# Shadow Audit — Realistic Fill GHOST Performance Scripts

Re-runs the GHOST strategy shadow performance audit using **actual ticks_clob ask prices** at the moment each fire was evaluated, rather than a fixed median fill.

## Files

| File | Purpose |
|------|---------|
| `shadow_audit_realistic_fills.py` | Main Python script — runs full audit, prints report, saves RDS notes |
| `shadow_audit_realistic_fills.sql` | Standalone SQL — same logic, parameterized with `:audit_date` |
| `run_shadow_audit.sh` | Wrapper script — sources credentials, runs Python script |

## Quick Start

```bash
# Run for today (saves notes to RDS automatically)
bash scripts/analysis/run_shadow_audit.sh

# Run for a specific date
bash scripts/analysis/run_shadow_audit.sh 2026-05-26

# Run without saving notes (dry run)
cd /path/to/novakashmain
source scripts/cross-compare/_lib.sh
export DB_HOST DB_PORT DB_USER DB_PASS DB_NAME
python3 scripts/analysis/shadow_audit_realistic_fills.py --date 2026-05-25 --no-save

# Raw SQL via psql
source scripts/cross-compare/_lib.sh
psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME \
  -v audit_date="'2026-05-25'" \
  -f scripts/analysis/shadow_audit_realistic_fills.sql
```

## Methodology

### Fill Price Source

1. **Primary**: `ticks_clob` table — CLOB order book polled every ~10s
   - UP/YES direction → `up_best_ask`
   - DOWN/NO direction → `down_best_ask`
   - Matched using 10s epoch buckets (±10s tolerance) for performance
2. **Fallback**: `market_data.up_price` / `down_price` when ticks_clob has no coverage

### Evaluation Time

For each fire: `eval_time = window_ts + 300 - eval_offset`
- `window_ts`: window start (Unix seconds, 5min granularity)
- `+300`: window duration (5 minutes)
- `-eval_offset`: seconds before close when signal was evaluated

### Deduplication

`DISTINCT ON (strategy_id, asset, window_ts)` ordered by `eval_offset ASC` — takes the earliest-possible entry for each (strat, asset, window) combination.

### PnL Formula

```
WIN:  stake * (1/fill_price - 1 - 0.072)   # 7.2% Polymarket crypto fee on stake
LOSS: -stake
FLAT: 0  (unresolved windows)
```

Break-even fill at 80% WR: approximately $0.56 (vs $0.84 median assumed in note #667)

### Promotion Criteria

A strategy is flagged as a **promotion candidate** when all of the following hold:
- WR ≥ 78%
- Shadow PnL at $5 stake > $0
- Fires ≥ 20 today
- Not already in the promoted set (`ALREADY_PROMOTED` list in the script)

## Performance Notes

The LATERAL join approach timed out at 120s. The current implementation uses a **10s bucket pre-aggregation** on `ticks_clob` that reduces the join from O(N×M) to O(N+M), completing in ~30–60s.

## Comparison vs Note #667

| Assumption | Note #667 | This audit |
|-----------|-----------|------------|
| Fill price | $0.84 median (all fires) | Actual ticks_clob ask at eval_time |
| Direction | Ignored | YES/UP → up_best_ask; NO/DOWN → down_best_ask |
| Stake | $25 | $25 AND $5 |
| Asset | Ignored | Per-fire from ticks_clob by asset |

Key impact: ETH DOWN strategies may have fills of $0.10–$0.35 (very different EV from $0.84). Late-window YES strategies on volatile BTC may see $0.85–$0.95 fills.

## Updating the Promoted Set

Edit `ALREADY_PROMOTED` in `shadow_audit_realistic_fills.py`:

```python
ALREADY_PROMOTED = {
    'v9_2_v12_AND_ghost',
    'v12_btc_ghost',
    'v9_btc_ghost',
    # add newly promoted strategies here
}
```

## RDS Note Tags

The script saves two notes on each run:
- **Note A** (analysis): tags `ghost-audit, shadow-performance, realistic-fills, YYYY-MM-DD`
- **Note B** (SQL library): tags `shadow-audit, sql-library, reusable-analysis, maintenance`
