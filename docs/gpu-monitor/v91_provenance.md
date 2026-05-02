# v9.1 LGB — PROVENANCE (immutable lineage)

**Last updated:** 2026-05-02
**Owner:** Billy (no auto-promote rule, post-2026-04-17)
**Status:** SHADOW-CANDIDATE (not yet wired in engine; awaiting v9_1_lgb_only strategy + timesfm-service v9.1 loader)

## What this is

v9 PROD (`candidate_ab83564`) retrained on Polymarket-aligned `delta_*` features. Same Sequoia v5 architecture: 78 features, 63 leaves, 0.03 lr, depth=-1, per-delta heads. The ONLY difference is the `delta_*` family + `ret_since_open` were recomputed using Gamma `eventMetadata.priceToBeat` (Chainlink Streams off-chain feed = Polymarket's actual resolution oracle) instead of `chainlink_polygon` (on-chain Aggregator V3, the legacy approximation).

## Why

Engine PR #464 (OPEN, not merged) found that `chainlink_polygon` ≠ Polymarket's resolution reference. Mean delta $14.68/window ≈ 1.9 bps. v9 PROD's 20 features in the `delta_*` family all rooted in the wrong reference.

## Training inputs

- **Dataset source:** `/tmp/retrain/regen_full.parquet` (derived from `s3://bbrnovakash-models-do-not-delete/v10_training_backup/backfill_data/se_full_dump_20260430_151532.parquet` via Gamma `eventMetadata.priceToBeat` join)
- **Filter:** TRADE-only (`decision = 'TRADE'`)
- **Label:** `outcome` (resolved Polymarket window, UP/DOWN)
- **n_rows TRAIN_POOL:** 117,827 (chronologically: window_ts < ts_max - 7d)
- **n_rows HOLDOUT:** 77,043 (last 7d, chrono-ordered, NOT in train_pool)
- **Training period:** ~2026-04-07 → 2026-04-25 (TRAIN_POOL)
- **Holdout period:** ~2026-04-25 → 2026-04-30 (HOLDOUT, used for chrono validation only)
- **p2b coverage:** TRAIN_POOL 100%, HOLDOUT 99.25%

## Hyperparameters (LightGBM)

```json
{
  "objective": "binary",
  "metric": "binary_logloss",
  "num_leaves": 63,
  "learning_rate": 0.03,
  "max_depth": -1,
  "verbose": -1,
  "seed": 42
}
```

Per-delta heads, simple time-ordered train/val split inside TRAIN_POOL (last 15% chrono = val for early-stop). 600-round cap, ES patience=30. Same hparams as v9 PROD `ab83564`.

## Feature list

78 features, Sequoia v5 schema. See `meta_v5.json`.

20 of them are in the `delta_*` family rooted in `open_price`:
- `delta_pct`, `delta_binance`, `delta_chainlink`, `delta_tiingo`
- `gate_delta_passed`, `delta_source_num`, `nested_5m_oi_delta_cumulative`, `source_delta_divergence`
- ewma12/ewma60/sg/roll_std20 variants of `delta_tiingo`, `delta_binance`, `delta_pct`

These were the BIASED features in v9 PROD and now use priceToBeat-derived base values in v9.1.

## Per-delta artifacts (5 deltas trained; d=030 falls back to v9 PROD)

| Delta | n (holdout) | best_iter | Test acc (post-iso) | ECE post-iso |
|---|---|---|---|---|
| 060 | 7,907 | 100 | 0.8084 (HONEST) | 0.0529 |
| 090 | 10,865 | 103 | 0.7643 (HONEST) | 0.0448 |
| 120 | 17,477 | 142 | 0.7773 (HONEST) | 0.0448 |
| 180 | 24,317 | 142 | 0.7566 (HONEST) | 0.0402 |
| 240 | 12,770 | 88 | 0.7309 (HONEST) | 0.0574 |

(d=030 not retrained: chrono split structure means TRAIN_POOL has no rows in eval_offset 15-45. d=030 inference falls back to v9 PROD booster via manifest, same fallback pattern v12 uses.)

## Validation: HONEST forward-pass (no contamination)

**Methodology:** chronologically split parquet by `window_ts`. TRAIN_POOL = first 18 days; HOLDOUT = last 7 days. Trained candidate on TRAIN_POOL only, scored on HOLDOUT (which the candidate had never seen). PROD models predate the regen window → trivially out-of-sample on holdout for them too. Symmetric, fair comparison.

### Per-delta v9 PROD vs v9.1 HONEST

| Delta | n | v9 PROD WR | v9.1 HONEST WR | Δ_WR | v9 PROD ECE | v9.1 ECE | Δ_ECE |
|---|---|---|---|---|---|---|---|
| 060 | 7,907 | 76.03% | 80.84% | **+4.81pp** | 0.062 | 0.053 | -0.009 |
| 090 | 10,865 | 74.18% | 76.43% | **+2.25pp** | 0.057 | 0.045 | -0.012 |
| 120 | 17,477 | 71.59% | 77.73% | **+6.14pp** | 0.061 | 0.045 | -0.016 |
| 180 | 24,317 | 68.52% | 75.66% | **+7.15pp** | 0.057 | 0.040 | -0.017 |
| 240 | 12,770 | 65.97% | 73.09% | **+7.12pp** | 0.046 | 0.057 | +0.011 |

**Weighted Δ_WR = +5.92pp** (n=73,336)

### Wilson 95% confidence intervals on lift

| Delta | Lift CI |
|---|---|
| 060 | +4.81pp ± 1.18pp |
| 090 | +2.25pp ± 1.07pp |
| 120 | +6.14pp ± 0.85pp |
| 180 | **+7.15pp ± 0.74pp** ← tightest, most certain |
| 240 | +7.12pp ± 1.04pp |

Every delta lift is statistically significant (CI does not cross zero).

## Known caveats

1. **Single 7-day regime in holdout** — Apr 23-30, 2026. Different market regimes (chop/cascade/risk-off) may behave differently. Live shadow tests this.
2. **d=240 ECE slightly worse** (+0.011). Worth watching during shadow.
3. **d=030 not retrained** — falls back to v9 PROD via manifest. Same pattern v12 uses for d=030/060.
4. **v9 PROD wasn't WF-CV trained** — but PROD predates holdout window so it's trivially out-of-sample. Methodology is clean despite this.
5. **First training run had data contamination** (validate.py used in-sample holdout). Inflated Δ_WR to +10.80pp. This honest re-run with chronological split corrected it. The contaminated artifacts at `s3://bbrnovakash-models-do-not-delete/v10_training_backup/retrain_2026-05-02/v9_1/` should NOT be promoted. Honest artifacts are at `.../honest/v9_1/`.

## Promotion criteria (gate before LIVE)

Shadow `v9_1_lgb_only` strategy (mode=GHOST) for ≥7 days alongside `v9_lgb_only` LIVE:

- n_decisions ≥ 5,000 on incoming windows
- shadow Δ_WR ≥ +2.0pp on aligned windows (within noise of honest +5.92pp)
- shadow ECE_max not worse by > 0.02 vs LIVE
- No regime where Δ_WR is significantly negative (CI lower bound < -1pp)
- Zero infra incidents (no skipped windows due to v9.1 model load failure)

## Reviewers / sign-off

- **Trained by:** claude-bg-agent (autonomous training run, 2026-05-02)
  - Initial run (contaminated): bg agent `abdd890338f739e91`
  - Honest forward-pass: bg agent `a75f2f74a69722dab`
- **Reviewed by:** Billy 2026-05-02 — accepted +5.92pp lift, approved S3 upload + shadow plan
- **Approved for shadow deploy:** PENDING — needs engine PR + timesfm sister PR + flag wiring
- **Approved for LIVE deploy:** PENDING — gates listed above

## Rollback procedure

1. **Server-side:** Set `V9_1_ENABLED=false` env on ML box, restart timesfm container. v9.1 scorer not instantiated, /v4/snapshot returns probability_lgb_v9_1=None.
2. **Engine-side:** Set `V9_1_LGB_ONLY_MODE=GHOST` (or remove from registry). Engine v9_1_lgb_only SKIPs gracefully when probability_lgb_v9_1 is None.
3. **Artifact-side:** v9 PROD `candidate_ab83564` continues running unchanged (still needed as feature provider for v12 per hub #286).

## References

- **Hub note #313** — full retrain context + honest forward-pass results + contamination postmortem
- **Hub note #295** — v9 feature provenance + stacking dependency map (v9 must keep running as feature provider)
- **Hub note #248** — v9 PROD `candidate_ab83564` retrain provenance (predecessor)
- **Hub note #286** — v12 vs v9 weight in production
- **Engine PR #464** — `fix(polymarket): use canonical priceToBeat for window open_price` (OPEN, not merged)
- **v12 PROVENANCE** — `s3://bbrnovakash-models-do-not-delete/v2/btc/v12/PROVENANCE.md` (the methodology this mirrors)
- **Honest artifacts:** `s3://bbrnovakash-models-do-not-delete/v10_training_backup/retrain_2026-05-02/honest/v9_1/`
- **Honest validation report:** `s3://bbrnovakash-models-do-not-delete/v10_training_backup/retrain_2026-05-02/honest/honest_validation_report.md`
- **Frozen training script:** `s3://bbrnovakash-models-do-not-delete/v10_training_backup/retrain_2026-05-02/honest/train_v9_1_honest.py`
- **Frozen validation script:** `s3://bbrnovakash-models-do-not-delete/v10_training_backup/retrain_2026-05-02/honest/score_holdout.py`
