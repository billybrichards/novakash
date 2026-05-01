# 15m Model Timeline

**Source:** `novakash-timesfm-repo` git log + S3 inventory of `s3://bbrnovakash-models-do-not-delete/`.

## Was a 15m classifier promoted? YES — 2026-04-20.

The user's recollection of "we made a new 15m classifier sometime" was correct. Full chronology below.

## Architecture introduction (April 18-20)

| Date (UTC) | Commit / PR | Event |
|---|---|---|
| 2026-04-18 12:30 | `e4b39a3` | `fix(forecaster): nested classifier_config schema + transformers/peft deps` |
| 2026-04-18 12:43 | `8b68799` | `fix(forecaster,v2_scorer): nested schema + state_dict prefix + patch padding + classifier_p_up passthrough` |
| 2026-04-18 13:16 | `4bf69a7` | `feat(forecaster,v2_scorer): unified head support — eval_offset concat + by-Δ dict` (precursor to PR #113) |
| 2026-04-19 19:53 | `e8f294a` | `fix(forecaster): classifier open_price bug + context_len cap` |
| 2026-04-20 18:53 | _S3 upload_ | **`timesfm_finetune/15m_v1/15m_v1/adapter_model.safetensors` (9.9 MB)** — 15m TimesFM LoRA fine-tune |
| 2026-04-20 19:05 | _S3 upload_ | **`timesfm_finetune/15m_head_v1/classifier_head.safetensors` (1.4 MB)** — 15m classifier head, val_dir_acc 75.37% |
| 2026-04-20 20:54 | PR #111 (`f68cf77`) | Merge classifier head + isotonic into main |
| 2026-04-20 21:18 | PR #113 (`0fd2b47`) | Unified head — eval_offset concat + by-delta dict |
| 2026-04-20 21:49 | `796f249` | `feat(forecaster): wire per-timescale classifier heads for 15m support` |
| **2026-04-20 22:36** | **PR #116 (`a0bd0bb`)** | **`feat(forecaster): per-timescale classifier heads (15m support)`** ⚡ |
| 2026-04-21 06:15 | PR #117 (`b3f272a`) | `fix(forecaster): auto-derive head horizon + defensive per-head loop` |
| 2026-04-21 06:27 | PR #118 (`ab83564`) | `feat(v4_snapshot): classifier-only fallback for ETH/SOL 15m (no LGB)` — this is the `classifier_only_mode` branch in `v7_15m_sniper.py`, line 537 |

## Trajectory architecture (April 24-25) — 5m only, never replaced 15m

| Date (UTC) | Commit | Event |
|---|---|---|
| 2026-04-23 21:15 | `e510141` | `fix(forecaster): v2 classifier arch matches training script` |
| 2026-04-24 15:40 | `b3d4fcc` | `feat(v2): wire contextual classifier head + subsample/horizon env toggles` |
| 2026-04-24 16:30 | PR #124 (`b6ef137`) | `fix(forecaster): v2 classifier arch matches training script` |
| 2026-04-24 16:31 | PR #126 (`47e56b1`) | `feat(v2): wire contextual classifier head + subsample/horizon env toggles` |
| 2026-04-24 20:05 | `48aa7a8` | `feat(ci): add deploy-classifier-box workflow + fast dataset builder` |
| 2026-04-24 21:56 | PR #129 (`d071781`) | `fix(forecaster): v2 classifier head uses correct 512×2s inference context` |
| 2026-04-24 22:53 | `31eeb6a` | `fix(classifier): serve eval-offset-correct delta instead of hardcoded T-60` |
| 2026-04-24 23:07 | `aa6d8d5` | `fix(classifier): inject live context (vpin/regime/ret_since_open) at snapshot time` |
| 2026-04-24 23:28 | `c8f3fbc` | `feat(classifier): 14-feature trajectory head + rolling context cache` ← cls_traj_14f |
| 2026-04-25 08:10 | `81841c0` | `fix(tiingo): catch AttributeError on Tiingo WS string ack` |
| 2026-04-25 09:32 | `f0692d8` | `fix(classifier): trajectory context features available from window start` |
| 2026-04-25 16:37 | `a037c62` | `feat(classifier): post-hoc isotonic calibration for v2 head` ← cls_traj_14f_iso |

**S3 artifacts from this wave (all 5m):**

| S3 path | Date | Val dir_acc | ECE | Notes |
|---|---|---|---|---|
| `path1_classifier/2026-04-18/unified_head/` | Apr 18 | 70.0% | ? | **Currently LIVE on `16.52.14.182` (timesfm-v2)** |
| `path1_classifier/2026-04-22/5m_head_v2/` | Apr 22 | ? | ? | Intermediate |
| `path1_classifier/2026-04-24T18-43/` (5m_head_v2_regime_fix) | Apr 24 | 78.6% HC | ? | Regime fix, S3 only |
| `path1_classifier/2026-04-24T22-09/cls_tiingo_2h/` | Apr 24 | 71.9% | ? | 2h context |
| `path1_classifier/2026-04-24T22-09/cls_tiingo_9h/` | Apr 24 | 71.3% | ? | 9h context |
| `path1_classifier/2026-04-24T22-35/` (14f-512) | Apr 24 | 79.5% HC | ? | 14-feature trajectory |
| `path1_classifier/2026-04-25T05-27/cls_traj_14f_2h/` | Apr 25 | 80.3% HC | ? | 14-feat traj 2h |
| **`path1_classifier/2026-04-25T15-34/cls_traj_14f_iso/`** | Apr 25 | **72.86% / 79.85% HC** | **5.2e-9** | **BEST — configured on GPU box, never promoted to live primary** |

## 15m artifacts (the actual answer)

```
s3://bbrnovakash-models-do-not-delete/timesfm_finetune/
├── 15m_v1/15m_v1/                          (LoRA — TimesFM fine-tune for 15m)
│   ├── README.md
│   ├── adapter_config.json
│   ├── adapter_model.safetensors           (9.9 MB)
│   └── fine_tune_metrics.json
└── 15m_head_v1/                            (classifier head — what 15m uses)
    ├── classifier_config.json              (input_dim=1280, hidden=[256,64], dropout=0.2)
    ├── classifier_head.safetensors         (1.4 MB)
    └── training_metrics.json               (val_dir_acc 75.37% best, 22 epochs)
```

**`15m_head_v1` validation curve** (from training_metrics.json):

| Epoch | val_dir_acc |
|---|---|
| 1 | 73.50% |
| 4 | 74.70% |
| 5 | 74.70% |
| **8** | **75.37%** ← best |
| 11 | 74.70% |

Training time: total ~2 sec across 22 epochs (very small head, training was fast).

## 15m LGB models (separate from classifier — these provide `probability_lgb`)

`v2/btc/btc_15m/` has 17 commits. Production = `a547c3d` from 2026-04-16.

```
s3://bbrnovakash-models-do-not-delete/v2/btc/btc_15m/a547c3d/2026-04-16T04-29-55Z/
├── lgb_btc_060.txt   isotonic_btc_060.json
├── lgb_btc_120.txt   isotonic_btc_120.json
├── lgb_btc_180.txt   isotonic_btc_180.json
├── lgb_btc_300.txt   isotonic_btc_300.json
├── lgb_btc_480.txt   isotonic_btc_480.json
└── lgb_btc_720.txt   isotonic_btc_720.json
```

These provide `probability_lgb` for BTC 15m (e.g. `0.243` at last poll). 6 horizons covering the entire T-720 to T-60 entry window.

**No 15m LGB exists for ETH/SOL/XRP.** `v2/eth/`, `v2/sol/`, `v2/xrp/` only have 5m + 1h + 4h, not 15m.

## What's serving WHERE right now (post-restart)

| Box | IP | Container | Classifier head | LGB heads | Notes |
|---|---|---|---|---|---|
| `novakash-timesfm-v2` (live primary) | `16.52.14.182` | Up 17h healthy | **`path1_classifier/2026-04-18/unified_head/`** ← OLD | btc 5m+15m+1h+4h all loaded | Engine queries this every few sec |
| `novakash-classifier-gpu` | `3.96.151.28` | **Up 2 minutes** (restarted today) | **`cls_traj_14f_iso`** (5m, BEST) + `15m_head_v1` (15m) configured | Same | **Restarted 2026-05-01 17:13 UTC** |
| `timesfm-classifier-shadow` | `16.54.184.137` | Up | Shadow | Shadow | Old CPU classifier box |
| `novakash-timesfm` (older) | `3.98.114.0` | Up | Older | Older | Pre-v2 stack |

**ETH/SOL/XRP `v2/.../current_15m_binance.json`** → `NoSuchKey` on S3 confirmed in GPU box logs at 17:13:55. **No 15m LGB models exist for non-BTC.**

## Recent retrains (last 36h) — all 5m

| Date (UTC) | S3 path | Type | Result |
|---|---|---|---|
| 2026-04-30 15:24 | `v11_training/20260430_151825/` | LGB 57f, 6 horizons | T-30 acc=79.5%, T-60=76.7%, T-90=75.5% |
| 2026-04-30 18:16 | `v12_training/20260430_171615_corrected_outcome_label/` | LGB v9+v12 features, 4 horizons | "corrected_outcome_label" — fixed label bug |
| **2026-05-01 00:03** | **`v12xl_training/boosters_20260501_000300/`** | **LGB v10 spec 112f + nested_5m_*** | **T-90 post-iso 78.0%** ("ship_heldout_iso") |

Per-tick eval today (note #307) on the deployed v12: blended 78.89% WR over 1.1M trade ticks. v12 alone 75.94%. v9_lgb_only 75.35%. **5m system is healthy.**

## Anomaly: classifier same value across 5m + 15m on GPU box

Post-restart `/v4/snapshot?asset=BTC` returns:

```
5m:  probability_classifier = 0.38608115911483765
15m: probability_classifier = 0.38608115911483765   ← IDENTICAL
```

**Six decimal places match.** This means the per-timescale classifier dispatch (PR #116) is not actually picking the 15m head — both timescales are getting the 5m head's output. **`15m_head_v1` IS configured but NOT being served.**

This is a separate issue from the engineering work the user remembers — the head WAS trained and the env var IS set, but the runtime dispatch is broken. Tracked as new audit task in the recommendation.

## Pending (not yet trained)

The `claude/15m-phase3-nested-features` and `claude/15m-parquet-data-source` branches in the timesfm-repo suggest in-progress work for a richer 15m feature set, but no S3 artifacts have landed yet. **A v12-XL-style 15m retrain is the highest-leverage future work.**
