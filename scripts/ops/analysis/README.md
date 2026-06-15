# Alpha-Mining Analysis Suite

Reusable Python scripts for mining the Novakash 5-min BTC engine's signal
surface (49 columns on `signal_evaluations` + LGB v9/v10/v12 from
`window_evaluation_traces.surface_json`).

Built 2026-05-01 to replace one-off bg-agent runs that previously cost ~10min
each. Now: `python3 scripts/ops/analysis/<name>.py --hours 87`.

## Scripts

| script | purpose |
|---|---|
| `signal_alpha_scanner.py` | Quintile sweep every numeric column + bool/categorical scan. Find single-signal alpha cells (T-band x regime x dir). |
| `gate_gain_loss.py` | For each baseline gate-set, ADD/REMOVE candidate gates. WR delta + P&L delta. |
| `combo_miner.py` | Greedy 2-way (and optional 3-way) AND-combos of atomic gates. Surface combos with Wilson_low > 0.6. |
| `regime_cascade_specialist.py` | Drilldown into CASCADE regime — which signals predict within it, CG-liquidation buckets. |
| `coinglass_direct_alpha.py` | Does CG carry RAW directional alpha independent of LGB? |
| `time_decay_analyzer.py` | Per-signal accuracy decay across T-bands. ASCII charts. |

All scripts share `_common.py` (Wilson CI, real-PnL math, T-band SQL CASE,
output writer).

## Common CLI

```
--hours N        lookback (default 87)
--asset BTC      asset filter (default BTC)
--timeframe 5m   timeframe filter (default 5m)
--stake 7.5      $ stake for real-pnl math (default 7.5)
--out-dir PATH   override output dir (default docs/analysis/auto/<TODAY>/)
```

## Conventions

- **Real P&L math**: `pnl_usd` from DB is broken (regression #8). All scripts
  recompute: `WIN = stake * (1 - fill) / fill - 0.072 * stake`, `LOSS = -stake`.
- **Wilson 95% CI** on every WR; rows with `n < 30` flagged `EXP` (exploratory).
- **T-bands** are seconds-to-close: T-30 (closest to resolution) -> T-240 (far back).
- **Multiple comparisons**: thousands of cells get tested, expect ~5% false
  positives at p=0.05. Trust only Wilson_low > 0.65 with n > 50, OR replicated
  across runs.
- **Outcome ground truth**: `signal_evaluations.outcome` (post PR #441,
  98.9% populated) or `window_snapshots.outcome` (resolution oracle).

## Where to run

- **Montreal** (preferred): RDS env loads from
  `/home/novakash/novakash/engine/.env`.
- **Mac**: scripts will use `engine/.env.local` if present, but Mac DATABASE_URL
  is sometimes stale (Railway proxy hostname can be wrong). Always sanity-check
  row-counts vs Montreal.

## Output

Markdown report + raw CSV (where applicable) to
`docs/analysis/auto/<DATE>/<script_name>.{md,csv}`.

## Known limitations

- `probability_lgb_v12` on `signal_evaluations` is 0% populated (writer
  regression #6 / audit #332). All v9/v10/v12 scans use
  `window_evaluation_traces.surface_json` JSON-extract.
- Implied fill = `clob_up_ask` / `clob_down_ask` snapshot — not realised FOK
  fill. Adequate for relative comparisons.
- `v_signal_comparison` view reads from `window_snapshots` and only covers
  v9/v12 (no v10) — we bypass it and read surface_json directly for full
  coverage.

## Memory references

- `feedback_payoff_math.md` — fill regime + break-even math
- `feedback_wallet_truth_authority.md` — DB pnl_usd unreliable
- `project_strategy_ledger.md` — prior strategy ledger
- Hub notes: #287, #298, #301, #302, #305, #307, #308

## Adding a new script

1. Drop a new file in `scripts/ops/analysis/`.
2. Import shared helpers from `_common.py`.
3. Take the standard CLI flags.
4. Write markdown + optional CSV via `write_output()`.
5. Document in this README + add to suite-runner if appropriate.
