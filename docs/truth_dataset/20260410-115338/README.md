# Truth Dataset Export

Generated: `2026-04-10T11:53:47.797553+00:00`  

Lookback: `36.0h` (since `2026-04-08T23:53:38.888426+00:00`)  


## Files in this directory

| File | Description | Rows |
|------|-------------|------|
| `poly_fills.csv` | Ground-truth CLOB fills from Polymarket data-api (append-only) | 203 |
| `poly_fills_enriched.csv` | poly_fills LEFT JOINed with trade_bible + signal_evaluations (one row per fill with engine context) | 203 |
| `trade_bible.csv` | Engine-side resolved trade records | 130 |
| `signal_evaluations.csv` | Every 2s TRADE/SKIP decision with gate context | 28695 |
| `gate_audit.csv` | Per-window gate decision audit | 28694 |
| `summary.json` | Aggregates + integrity check | — |

## Integrity check

- Actual BUY gross (on-chain): **$716.73**
- Recorded stake (trade_bible): **$489.88**
- **Unrecorded spend**: **$226.85**
- Verdict: **BAD — multi-fill bug active, spend drifting from recorded stake**

## Multi-fill breakdown

- Single-fill windows: 23
- Double-fill windows: 15
- Triple+ fill windows: 50
- **Multi-fill %**: 73.9%
- Total BUY gross: $716.73

## Trade performance

- Total trades: 130
- Wins: 85
- Losses: 45
- WR: **65.4%**
- Recorded P&L: **$-35.85**

## Loading into pandas

```python
import pandas as pd
from pathlib import Path

d = Path('docs/truth_dataset/<THIS_DIR>')  # replace with actual timestamp
fills = pd.read_csv(d / 'poly_fills.csv', parse_dates=['match_time_utc', 'verified_at', 'created_at'])
enriched = pd.read_csv(d / 'poly_fills_enriched.csv', parse_dates=['match_time_utc', 'placed_at', 'resolved_at', 'se_evaluated_at'])
tb = pd.read_csv(d / 'trade_bible.csv', parse_dates=['placed_at', 'resolved_at', 'created_at'])
se = pd.read_csv(d / 'signal_evaluations.csv', parse_dates=['evaluated_at'])

# Example: multi-fill windows joined with the engine's decision context
multi = enriched[enriched['is_multi_fill'] == True]
print(multi.groupby('multi_fill_total')['cost_usd'].agg(['count', 'sum', 'mean']))

# Example: WR by regime
tb_wr = tb.groupby('regime')['trade_outcome'].value_counts().unstack().fillna(0)
tb_wr['wr'] = tb_wr.get('WIN', 0) / (tb_wr.get('WIN', 0) + tb_wr.get('LOSS', 0))
print(tb_wr)
```

## Refreshing this export

```bash
cd /Users/.../brave-archimedes
DATABASE_URL='postgresql://...@hopper.proxy.rlwy.net:35772/railway' \
  python3 scripts/export_truth_dataset.py --hours 36
```

The script is read-only against Railway — safe to run anytime.