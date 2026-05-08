# scripts/sim — Offline replay tools

## tick_simulator.py

Tick-by-tick gate replay simulator. Fixes the ~60x looseness of SQL-based gate
simulations (`signal_swap_backtest.sql`) by importing actual engine gate logic
and replaying `signal_evaluations` history chronologically with stateful
cooldown, consecutive-pass, and dedup counters.

**Hub note #348 section 4** — full plan and risk register.
**Audit task #381** (`audit-2026-05-06-09`).

### Why this exists

Memory `feedback_strategy_comparison_tband_convention.md` documents that SQL
gate sims are **~60x looser** than the live engine because they omit:
- Post-loss cooldown (20 min default)
- 3-tick consecutive confirmation gate
- Per-window dedup (one trade per window per direction)
- Fill band gates
- Source agreement (chainlink + tiingo NULL-block)
- Per-direction UTC hour blocks

Without accurate gate replay, combo-mining and signal-swap recommendations
from SQL analysis are speculative.

### Gate coverage

| Gate | Source | Replayed? |
|------|--------|-----------|
| G1 Timing (eval_offset bounds) | YAML `min_offset_sec`/`max_offset_sec` | YES |
| G2 UTC hour block | YAML `blocked_utc_hours` | YES |
| G2b Per-direction UTC hour block | YAML `blocked_utc_hours_up/down` | YES |
| G3 Regime (v4) | YAML `tradeable_v4_regimes` | YES (low confidence — see caveat) |
| G4 Source agreement | `signal_evaluations.delta_*` | YES |
| G5 VPIN guard | `signal_evaluations.vpin` | YES |
| G6 LGB probability | `signal_evaluations.probability_lgb_v9_1/v12` | YES |
| G7 Direction agreement | model directions | YES |
| G8 LGB safety floor | `lgb_dist_min_up/down` | YES |
| G9 Fill band | `signal_evaluations.clob_up/down_ask` | YES |
| G10 Post-loss cooldown | stateful (initialized to 0) | YES |
| G11 3-tick confirmation | stateful | YES |
| G12 Per-window dedup | stateful | YES |
| ChainlinkFreshnessGate | age not in signal_evaluations | NO |
| OracleDisagreeGate | oracle_direction not per-tick | NO |
| CellPauseGate | rolling-WR state | NO |
| BlockCellsGate | per-cell predicates | NO |

### Regime replay caveat

`window_snapshots.regime` has a known writer regression (99.7% NULL —
`project_mass_combo_2026_05_01.md`). The simulator uses
`signal_evaluations.regime` (VPIN regime) as a proxy.  All outputs include
`regime_replay_confidence: LOW` until audit #12 + writer fix lands.

### Usage

**On Montreal** (has DATABASE_URL pointing at RDS prod):

```bash
cd /home/novakash/novakash

# Basic 7d replay of v12_lgb_combo
python3 scripts/sim/tick_simulator.py \
    --strategy-id v12_lgb_combo \
    --hours 168 \
    --validate

# Replay with config override (e.g. test adding Binance to source agreement)
cat > /tmp/audit1_override.json << 'EOF'
{"oracle_agreement_min_sources": 3}
EOF

python3 scripts/sim/tick_simulator.py \
    --strategy-id v12_lgb_combo \
    --hours 168 \
    --gate-config-override /tmp/audit1_override.json \
    --output-json /tmp/sim_audit1.json \
    --validate

# Compare two configs side-by-side
python3 scripts/sim/tick_simulator.py --strategy-id v12_lgb_combo --hours 168 \
    --output-json /tmp/sim_baseline.json

python3 scripts/sim/tick_simulator.py --strategy-id v12_lgb_combo --hours 168 \
    --gate-config-override /tmp/audit1_override.json \
    --output-json /tmp/sim_with_override.json
```

### DB validation test (Montreal)

```bash
cd /home/novakash/novakash/engine
DATABASE_URL=$(grep DATABASE_URL /home/novakash/novakash/engine/.env | cut -d= -f2-) \
    python3 -m pytest tests/integration/test_tick_simulator_validation.py \
    -m requires_db -v
```

### Gate config keys

All keys with defaults (mirrors `v12_lgb_combo.yaml` gate_params):

| Key | Default | Description |
|-----|---------|-------------|
| `min_offset_sec` | 24 | Min eval_offset (sec-to-close) |
| `max_offset_sec` | 240 | Max eval_offset |
| `tradeable_v4_regimes` | all 4 | Allowed v4 regimes |
| `blocked_utc_hours` | [] | Symmetric UTC hour blocks |
| `blocked_utc_hours_up` | [] | UP-direction hour blocks |
| `blocked_utc_hours_down` | [] | DOWN-direction hour blocks |
| `source_agreement_require_chainlink` | true | Block if chainlink NULL |
| `source_agreement_require_tiingo` | true | Block if tiingo NULL |
| `oracle_agreement_min_sources` | 2 | Min sources to agree on direction |
| `vpin_min` | 0.40 | Min VPIN to allow trade |
| `vpin_max` | 1.0 | Max VPIN to allow trade |
| `fill_band_min` | 0.00 | Global fill floor |
| `fill_band_max` | 0.82 | Global fill ceiling |
| `up_min_fill_price` | 0.20 | UP direction fill floor |
| `down_min_fill_price` | 0.15 | DOWN direction fill floor |
| `post_loss_cooldown_min` | 20 | Minutes of cooldown after LOSS |
| `min_consecutive_pass_ticks` | 3 | Ticks required before firing |
| `combo_min_dist` | 0.10 | Min min(dist_v9_1, dist_v12) for agreement path |
| `lgb_dist_min_up` | 0.13 | UP LGB safety floor |
| `lgb_dist_min_down` | 0.10 | DOWN LGB safety floor |
| `v12_contrarian_enabled` | true | Enable v12 contrarian path |
| `v12_contrarian_min_dist` | 0.10 | Min v12 dist for contrarian |

### Validation tolerance (Hub #348)

- ±2 trades on any single (window_ts, direction) pair
- ±5% on aggregate n_fires per calendar day
- First 30 minutes discarded (cooldown/consec state warm-up)

### Output

```
=== Simulated fires per cell ===
strategy_id    direction  t_band      vpin_regime  n_would_fire  n_resolved  simulated_WR  fill_adj_EV  avg_fill  regime_replay_confidence
v12_lgb_combo  UP         T-121-180   TRANSITION   12            10          80.0%         $24.50       0.550     LOW
v12_lgb_combo  DOWN       T-181-240   CASCADE      8             7           85.7%         $18.20       0.420     LOW
...
```

## replay_cell_pauses.py

Pre-deploy validator for the rolling-WR cell-pause gate. See inline docstring
for usage.
