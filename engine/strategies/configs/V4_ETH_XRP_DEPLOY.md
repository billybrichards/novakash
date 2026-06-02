# V4 ETH/XRP TickFormer — Engine PR

> Sister to classifier-gpu PR `feat/v4-eth-xrp-emit` (timesfm repo).
> RDS sources: hub note #823 (chain-complete + sweep), #818 (runbook).

## What this PR adds

- **8 strategy configs** mirroring BTC tickformer family, asset-suffixed:
  - `tickformer_v{16,17,18,20}_{pure,sniper,t180,adaptive_early}_eth`
  - `tickformer_v{16,17,18,20}_{pure,sniper,t180,adaptive_early}_xrp`
- **8 new FullDataSurface fields**: `probability_tickformer_v{16,17,18,20}_{eth,xrp}` (data_surface.py L427+)
- **No runtime overrides migration in this PR.** GHOST soak baseline = YAML top-pocket defaults (single threshold per strat, asset-conditional). Per-band override SQL was dropped after code review found the assumed `strategy_runtime_overrides(strategy_id, asset, eval_offset_band, direction, ...)` schema doesn't match prod (actual: `strategy_id PK, mode, params jsonb`). Adding per-band tuning requires either (a) JSONB-shaped overrides + reader change in `_tickformer_base.evaluate_tickformer_strategy()`, or (b) schema extension. Deferred to a follow-up PR once GHOST soak data confirms the YAML defaults are roughly right. RDS note #827 has the full reasoning.

## Cross-repo contract

The 8 new `probability_tickformer_v{N}_{asset}` columns are emitted by the
classifier-gpu sister PR on the timesfm repo. Engine fields default `None`
so this PR is **forward-compatible** — strats land safely and SKIP cleanly
with `tickformer_v{N}_{asset}_not_available` reason until emission is live.

## Asset gate

YAML `asset: ETH` / `asset: XRP` (not `ANY`) so each new strat runs ONLY
on its asset's windows. BTC strats stay `asset: ANY` (model is cross-asset).

## All shadow_only=1 (GHOST soak)

Per `feedback_no_auto_promote`. Even after the column emits a non-None
probability, the `_tickformer_base.evaluate_tickformer_strategy()` core
honours `gate_params.shadow_only=1` and returns SKIP-with-record. Billy
flips `shadow_only=0` per-strategy after soak telemetry confirms WR.

## Top-pocket defaults per (asset, variant)

From `tf_v4_pocket_sweep.json` (RDS note #823 §2):

| Variant | ETH default | XRP default |
|---|---|---|
| v16_pure | up=0.84 (EO 60 UP WR=0.970) | up=0.88 (EO 60 DN WR=1.000) |
| v17_sniper | up=0.88 (EO 80 UP WR=0.984) | up=0.86 (EO 60 DN WR=1.000) |
| v18_t180 | up=0.76 (EO 60 UP WR=0.951) | up=0.90 (EO 60 DN WR=1.000) |
| v20_adaptive_early | up=0.78 (EO 60 UP WR=0.953) | up=0.90 (EO 60 DN WR=0.978) |

`down_threshold` is set symmetrically (1 - up_threshold) per
`reference_tickformer_bidirectional`. Runtime overrides (the SQL migration)
expand this with per-band thresholds — the YAML default is the fallback
when no override row exists for a (strategy, asset, band, direction) cell.

## Runtime overrides shape

```sql
INSERT INTO strategy_runtime_overrides (
  strategy_id, asset, eval_offset_band, direction, prob_threshold,
  shadow_only, source_hub_note, source_cell_n, source_cell_wr,
  source_cell_wilson, created_at
) VALUES (
  'tickformer_v16_pure_eth', 'ETH', '60-80', 'UP', 0.84,
  1, 823, 99, 0.9697, 0.9147, NOW()
) ON CONFLICT (strategy_id, asset, eval_offset_band, direction) DO UPDATE ...;
```

49 rows total. Bands: `60-80`, `100-140`, `160-180`, `200-220`.

## Deploy order

1. **classifier-gpu PR ships first** (timesfm `feat/v4-eth-xrp-emit`).
   Adds 8 ckpts to S3 → env vars → asset-routed scorer → column emission.
2. **Verify emission** via `GET /v4/snapshot?asset=ETH&timescale=5m` —
   `timescales.5m.probability_tickformer_v16_eth` should be non-None on
   any window with sufficient tick buffer.
3. **Engine PR ships second** (this PR). 8 strats + data_surface fields
   + runtime override migration. Strats fire in GHOST.
4. **Apply migration**: `psql -f engine/migrations/2026_06_02_v4_eth_xrp_tickformer_overrides.sql`
5. **Verify shadow fires**: query `signal_evaluations WHERE strategy_id LIKE 'tickformer_%_eth' OR strategy_id LIKE 'tickformer_%_xrp'`
6. **Soak**: Billy lets it run 1-2 weeks. Watches WR per cell vs offline.
7. **Promote individually**: flip `gate_params.shadow_only=0` in each
   strat's YAML once shadow WR confirms (per-strategy, never bulk).

## NOT in this PR (deferred)

- Sidecar writers for `probability_tickformer_v{N}_{asset}` columns in
  `signal_evaluations` table. Currently the columns don't exist there —
  shadow fires write decision rows but not the prob. **Decision**: add
  a single JSONB sidecar field `tickformer_v4_probs` to avoid 8 column
  migrations. Follow-up note when sidecar writer needed.
- Exit monitor `tickformer_prob_reader` extension for asset-suffixed v20.
  Not on critical path — exit monitor only watches BTC v20 today.

## Files

```
NEW:  engine/strategies/configs/tickformer_v{16,17,18,20}_{pure,sniper,t180,adaptive_early}_{eth,xrp}.{yaml,py}  (16 files)
MOD:  engine/strategies/data_surface.py  (8 new fields + 8 assembly blocks)
NEW:  engine/migrations/2026_06_02_v4_eth_xrp_tickformer_overrides.sql  (49 INSERT)
NEW:  engine/strategies/configs/V4_ETH_XRP_DEPLOY.md  (this doc)
```

## Sister PR refs

- **classifier-gpu (timesfm)**: `feat/v4-eth-xrp-emit` — TBD, see
  `magic-model/V4_DEPLOY_PLAN.md` and `CLASSIFIER_GPU_PLAN.md`.
- **S3 ckpts**: `s3://bbrnovakash-models-do-not-delete/training_caches/tickformer/{ETH,XRP}/2026-06-02/v4/`
- **RDS hub notes**: #818 (runbook), #822 (early finding), #823 (final)
