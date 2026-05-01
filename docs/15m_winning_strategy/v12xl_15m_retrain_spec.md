# v12-XL-15m Retrain Spec

**Audit:** #347
**Authored:** 2026-05-01
**Status:** SPEC — awaits owner assignment + training compute
**Owner:** TBD (likely sister-repo agent)

## Why this retrain

The 5m LGB system is on its 6th iteration (v2 → v5 → v9 → v10 → v11 → v12 → v12-XL). The 15m LGB system is frozen on `lgb_btc_15m__a547c3d` from 2026-04-16 — 16 days old, 78 features, no nested aggregates, no isotonic-per-horizon. **v12-XL-15m matches the 5m system's depth for 15m.**

Expected lift: 5-10pp WR ceiling on v15m_up_basic (currently 73% per-window deduped at 14d ghost).

## Architecture

Clone `v12xl_training/boosters_20260501_000300/train_v12xl_gpu.py` with these changes:

| Knob | 5m v12-XL | 15m v12-XL (proposed) |
|---|---|---|
| Algorithm | LightGBM binary | Same |
| Loss | binary_logloss | Same |
| `num_leaves` | 31 | 24 (smaller — less data) |
| `max_depth` | 6 | 6 |
| `learning_rate` | 0.005 | 0.005 |
| `min_data_in_leaf` | 100 | 60 |
| `lambda_l2` | 0.1 | 0.5 (more reg, smaller dataset) |
| `is_unbalance` | true | true |
| `feature_fraction` | 0.85 | 0.85 |
| `bagging_fraction` | 0.85 | 0.85 |
| `bagging_freq` | 5 | 5 |
| `n_estimators` | up to 5000 w/ early-stop@100 | Same |

## Horizons (seconds_to_close)

15m windows are 900s. Entry band per `v15m_up_basic` is T-540 to T-180. Train 4 horizons covering this band + a guard:

```
180  (= T-180 — band end, latest entry)
300  (= T-300 — middle)
480  (= T-480 — band start area)
720  (= T-720 — early/guard, before band opens)
```

This matches the existing `a547c3d` 6-horizon set partially (a547c3d has 60/120/180/300/480/720; we drop the unused 60/120 since 15m never enters those during the strategy's entry band).

## Features (112 + 4 NEW = 116)

Base 112 = same as 5m v12-XL (see `v12xl_summary.json`). Modifications for 15m:

### Replace `nested_5m_*` with `nested_15m_*`

Same statistic family but aggregated over the trailing **3 × 15m windows** instead of 5m sub-windows of a 15m window:

```
nested_15m_avg_prob_up        # rolling mean of probability_up across last 3 windows
nested_15m_prob_trend         # OLS slope of probability_up vs window index
nested_15m_gate_pass_rate     # fraction of last 3 windows that passed all upstream gates
nested_15m_regime_consistent  # 1 if v4_regime same in last 3 windows, else 0
nested_15m_vpin_peak          # max VPIN in last 3 windows
nested_15m_oi_delta_cumulative # sum of CG OI delta over last 3 windows
```

### NEW features (4)

```
hour_utc_sin              # sin(2π * hour / 24)
hour_utc_cos              # cos(2π * hour / 24)
realized_vol_60min        # std(returns) over trailing 60 min
is_us_session             # 1 if hour in {16,17,18,19,20,21}, else 0  (lets model learn the regime hard-gate)
```

**Rationale**: the 14d ghost analysis showed UTC 16-21 = 25-57% WR vs other hours 67-100%. The current v15m_up_basic uses a hard `block_hours_utc: [16-21]` gate. Adding these 4 features lets the model learn the regime structure directly. Hard gate stays as defense-in-depth in YAML.

## Training data

### Source

```sql
-- Primary table
SELECT
    se.window_ts, se.eval_offset, se.evaluated_at,
    se.probability_up, se.probability_lgb, se.probability_classifier,
    -- ... all 116 features ...
    md.outcome
FROM signal_evaluations se
JOIN market_data md
    ON md.window_ts = se.window_ts
    AND md.timeframe = '15m'
    AND md.asset = 'BTC'
WHERE se.timeframe = '15m'
  AND se.asset = 'BTC'
  AND se.evaluated_at > NOW() - INTERVAL '60 days'
  AND md.outcome IN ('UP', 'DOWN')
  AND se.eval_offset IN (180, 300, 480, 720)
```

### Caveat: `signal_evaluations` for 15m is sparse

Per note #172: BTC 15m has only 1,514 rows in `signal_evaluations` (last Apr 24 22:27 — STALE). The 5m v12-XL training pipeline filters on `trade_placed=true` which yields ~32k rows for v12-XL.

**For 15m, drop the `trade_placed` filter** — there are no 15m trades. Use ALL resolved windows. If `signal_evaluations` is too sparse, fall back to:

1. Compute features from the trailing tick tables (`ticks_chainlink`, `ticks_binance`, `ticks_tiingo`, `ticks_coinglass`) for each 15m window in `market_data` with outcome resolved
2. Replicate the offline backfill pattern from `v10_training_backup/scripts/build_dataset.py`

### Size estimate

- 14d × 96 windows/day × 4 horizons × 1 asset = **~5,400 rows** if `signal_evaluations` is current
- 60d × 96 × 4 = **~23,000 rows** if 60d backfill works
- For a 116-feature LightGBM, 23k rows is on the low side but workable with regularization. Compare: v12-XL 5m used ~32k rows.

### Data splits

```
Train:    Day -60 → Day -3
Val:      Day -3  → Day -1.5  (in-sample isotonic candidate)
Heldout:  Day -1.5 → Day 0    (out-of-sample isotonic CV — never touched until acceptance)
```

The `v12xl_heldout_iso_summary.json` pattern is `tail_days: 3` — same 3-day heldout for 15m. Per-horizon isotonic refits ON HELDOUT, then evaluated on holdout itself for ECE delta vs raw booster.

## Acceptance criteria (gate before promotion)

| Metric | Threshold | Why |
|---|---|---|
| Heldout dir_acc per horizon | ≥ 70% | Beats current `a547c3d` (~70% expected). 5m v12-XL T-90 hits 78% post-iso |
| ECE post-iso (heldout) | < 0.05 | Matches v12-XL 5m T-90 (0.119 was disappointing); v9 was 0.097 |
| Skill over null model | ≥ +20pp | Standard "model has signal" gate. Null = always-UP at 50.6% baseline (per market_data 14d) |
| HC band coverage @ \|p−0.5\|≥0.15 | ≥ 25% | 15m windows are noisier than 5m → lower than 5m's 43% target |
| Disagreement with `a547c3d` heldout | ≤ 30% | Sanity check that we haven't trained an entirely different model |

If ANY metric fails: do NOT promote. Iterate on hyperparams (start with feature subset reduction — drop nested_15m_* if unstable on small data) and retrain.

## Deploy plan

### S3 layout

```
s3://bbrnovakash-models-do-not-delete/v2/btc/btc_15m/v12xl_<train_sha>/<timestamp>/
    lgb_btc_180.txt
    lgb_btc_300.txt
    lgb_btc_480.txt
    lgb_btc_720.txt
    isotonic_btc_180.json
    isotonic_btc_300.json
    isotonic_btc_480.json
    isotonic_btc_720.json
    isotonic_btc_180_heldout.json     # per v12-XL pattern
    isotonic_btc_300_heldout.json
    isotonic_btc_480_heldout.json
    isotonic_btc_720_heldout.json
    train_v12xl_15m_gpu.py            # frozen training script
    v12xl_15m_summary.json            # acceptance metrics + hyperparams + features
    heldout_iso_summary.json          # per-horizon ECE/acc + ship verdict
```

### Pointer update

```
s3://bbrnovakash-models-do-not-delete/v2/btc/current_15m.json
```

Update to point at the new candidate manifest.

### Hot-reload (no restart)

```bash
curl -X POST http://16.52.14.182:8080/v2/admin/reload \
  -H "Authorization: Bearer $V2_RELOAD_TOKEN"
```

This re-reads `current_15m.json` and downloads + loads the new boosters + isotonic. Verified via:

```bash
curl http://16.52.14.182:8080/v4/health | jq .v2_btc_15m
```

Should show new `train_sha` in `model_version`.

### Shadow A/B (48h before cutover)

Don't replace `a547c3d` immediately. For 48h, serve BOTH models in parallel and compute on every resolved 15m window:

| Metric | Old (a547c3d) | New (v12xl) | Δ |
|---|---|---|---|
| dir_acc per horizon | | | |
| ECE per horizon | | | |
| HC coverage | | | |
| WR on actual `v15m_up_basic` ghost decisions (using new model's probability_up via shadow blend) | | | |

Promote ONLY if Δ_dir_acc ≥ +3pp AND ECE doesn't degrade.

### Cutover

After 48h shadow A/B passes acceptance:

1. Update `current_15m.json` to point at v12xl artifact
2. Hot-reload via `/v2/admin/reload`
3. Engine starts using v12xl on next /v4/snapshot call
4. Monitor v15m_up_basic ghost decisions for 24h
5. Promote `v15m_up_basic` LIVE (or re-promote if it's already LIVE) once new model is stable

## Risk register

| Risk | Mitigation |
|---|---|
| Sample size 1/3 of 5m → overfitting risk | Wider regularization (`lambda_l2: 0.5`), smaller `num_leaves: 24`, aggressive early stopping (patience 50) |
| `nested_15m_*` features need 3 prior windows of context — first 3 windows of training data are NULL | Drop these rows OR use trailing forward-fill OR omit the feature in v1 |
| Calibration drift like v10 (note #276) | Use isotonic ON HELDOUT only; acceptance criterion ECE < 0.05 prevents promotion if drifted |
| Hour features amplify regime exposure | Validate model isn't just "long during good hours" by checking direction-balance per hour bucket |
| Hot-reload race condition with engine queries | The `/v2/admin/reload` endpoint already handles this (existing pattern from v9/v10/v12 reloads) |
| Sister-repo agent training out-of-band, no review | Require PR + CI pass + Billy approval before pointer update (no auto-promotion per `feedback_no_auto_model_promotion.md`) |

## Estimated effort

| Phase | Time |
|---|---|
| Data extraction + feature engineering | 1 day |
| Training run + hyperparam tuning | 1-2 days (GPU compute) |
| Shadow A/B harness | 0.5 day |
| 48h shadow soak | 2 days (wall clock) |
| Cutover + monitoring | 0.5 day |
| **Total** | **~5-6 days** including soak |

## Cross-refs

- 5m v12-XL pattern: `s3://bbrnovakash-models-do-not-delete/v12xl_training/boosters_20260501_000300/`
- 5m v12-XL summary: `v12xl_summary.json` (112 features, hyperparams, deltas)
- 5m v12-XL heldout iso: `heldout_iso_summary.json` (T-90 78% ship_heldout_iso, T-120 68.6% ship_raw_booster)
- Per-tick eval today (note #307): blend hits 78.89% WR over 1.1M trade ticks → confirms 5m v12-XL is healthy in shadow
- Calibration discipline (note #286): "DO NOT PROMOTE if sample size <500" — 15m heldout iso must pass this gate too
- Hub note #172: "Classifier + 15m / multi-asset coverage — BTC-5m only today, infra is half-built"
