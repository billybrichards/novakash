# Infrastructure Snapshot — 2026-05-01 17:30 UTC

Live state of the boxes + models + data flow at the time of this analysis.

## EC2 instances (ca-central-1)

| Instance ID | Name | Type | Public IP | Status | Role |
|---|---|---|---|---|---|
| `i-05dbd2ca41f8a75ec` | novakash-timesfm | c6a.xlarge | 3.98.114.0 | running | Older TimesFM service (pre-v2 stack) |
| `i-013e50f55397120c4` | novakash-hub-v2 | t3.large | 16.54.141.121 | running 13d | **Hub API + Postgres-clients + macro-observer** |
| `i-0125675acf0e486e7` | **novakash-timesfm-v2** | **c6a.2xlarge** | **16.52.14.182** | **running 17d (healthy)** | **LIVE primary ML — engine queries here** |
| `i-0e20b7c239f018ad1` | timesfm-classifier-shadow | c6a.xlarge | 16.54.184.137 | running 7d | Shadow classifier box (CPU) |
| `i-0f77468732c4d8250` | **novakash-classifier-gpu** | **g4dn.xlarge T4 16GB** | **3.96.151.28** | **running 1d / container Up 2 min (restarted today)** | **GPU classifier box — has cls_traj_14f_iso configured** |
| `i-0785ed930423ae9fd` | novakash-montreal-vnc | t3.medium | **15.222.138.228** ⚠️ | running 7d | **Engine** — IP changed from 15.223.247.178 |
| `i-0fe72a610900b5cca` | novakash-frontend-v3 | t3.small | 99.79.41.246 | running 25d | Frontend + nginx proxy (currently broken — audit #329 surfaces) |

⚠️ **Montreal IP changed.** Previous notes (memory + lessons) reference `15.223.247.178` — current is `15.222.138.228`. Updating lessons.md.

## Live primary ML box (`16.52.14.182`)

```
Container: timesfm-api  Up 17h (healthy)
Image: novakash-timesfm:latest
Port: 0.0.0.0:8080->8080/tcp

ENV (relevant):
  TIMESFM_CLASSIFIER_HEAD_URI = s3://...path1_classifier/2026-04-18/unified_head/
  TIMESFM_LORA_ADAPTER_URI    = s3://...timesfm_finetune/candidate_ceb7fa3/2026-04-17T21-35Z-4assets-5m/
  V2_MODEL_CACHE_DIR          = /app/.cache/v2_models

Loaded LGB models (per `ls /app/.cache/v2_models/`):
  v2__btc__btc_15m__a547c3d__2026-04-16T04-29-55Z__lgb_btc_{060,120,180,300,480,720}.txt
  v2__btc__btc_15m_binance__61cbd6e__2026-04-15T19-24-24Z__lgb_btc_{060,120,180,300,480,720}.txt
  + 5m, 1h, 4h equivalents
  + isotonic calibrators per delta
```

**Engine queries this box continuously** via `http://15.222.138.228:.../v4/snapshot?asset={btc,eth,sol,xrp}&timescales=5m,15m`. Confirmed in container logs.

## GPU classifier box (`3.96.151.28`) — restarted today

```
Container: timesfm-api  Up 2 min (health: starting → healthy)
Image: novakash-timesfm:gpu (built locally from Dockerfile.gpu)
GPU: Tesla T4, 613 MiB used / 15 GB total

ENV (relevant):
  TIMESFM_CLASSIFIER_HEAD_URI       = s3://...path1_classifier/2026-04-25T15-34/cls_traj_14f_iso/
  TIMESFM_CLASSIFIER_HEAD_URI_15M   = s3://...timesfm_finetune/15m_head_v1/         ← 15m head!
  TIMESFM_LORA_ADAPTER_URI          = s3://...timesfm_finetune/lora_btc_5m_2h_2026-04-25T07-07/
  V2_SCORING_LOOPS_ENABLED          = false        ← no DB writes, lite mode
  V4_DB_WRITER_ENABLED              = false
  SCORER_ASSETS                     = btc           ← BTC only
  TIINGO_FEED_BUFFER_HOURS          = 10            ← 10h backfill buffer
  ISOTONIC_CALIBRATION_ENABLED      = false         ← isotonic OFF (interesting given iso head)
```

**Bugs found:**
1. `docker-compose.gpu.yml` is missing `restart: unless-stopped`. Container won't survive next reboot.
2. **Per-timescale classifier dispatch broken**: `/v4/snapshot?asset=BTC` returns IDENTICAL `probability_classifier` value for 5m and 15m timescales (`0.38608115911483765` to 17 decimals). The `15m_head_v1` env var is set but the runtime is using the 5m head for 15m too.

**S3 model load order on startup:**
```
17:13:55 Phase 4 scorer 15m loaded: assets=['btc']
17:13:55 v2 registry: no models for xrp btc_15m_binance (NoSuchKey)
17:13:55 v2 registry [15m/binance] loading eth from s3://.../v2/eth/current_15m_binance.json (NoSuchKey)
17:13:55 v2 registry [15m/binance] loading sol from s3://.../v2/sol/current_15m_binance.json (NoSuchKey)
17:13:55 v2 registry [1h/binance] loading btc from s3://.../v2/btc/current_1h_binance.json
... 
17:14:00 Phase 4 scorer 4h_binance loaded: assets=['btc']
```

**Confirms:** ETH/SOL/XRP have NO 15m or 5m LGB models at all. Audit #266 + #267 still blocking.

## Data flow

```
                            Tiingo + Binance +
                            Chainlink + CoinGlass
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │  16.52.14.182 (LIVE) │
                         │   timesfm-api :8080  │
                         │                      │
                         │   /v4/snapshot       │ ←──────── Engine polls
                         │   /v4/health         │           every 2-5s
                         │                      │           per asset/tf
                         │   path1_classifier/  │
                         │   2026-04-18/        │
                         │   unified_head/      │
                         └──────────┬───────────┘
                                    │
                                    │ writes
                                    ▼
                         ┌──────────────────────┐
                         │  Railway Postgres    │
                         │  (hopper.proxy)      │
                         │                      │
                         │  - market_data       │ ←── outcome resolution from
                         │  - signal_evals      │     Polymarket gamma + Chainlink
                         │  - strategy_decisions│ ←── written by engine
                         │  - trades            │
                         │  - window_snapshots  │
                         └──────────┬───────────┘
                                    │
                                    │ reads
                                    ▼
                         ┌──────────────────────┐
                         │   16.54.141.121      │
                         │   hub-api :8091      │
                         │                      │
                         │   /api/notes         │
                         │   /api/audit-tasks   │
                         │   /api/v58/...       │ ←── api/v58/strategy-decisions
                         │   /api/trades        │     BUG: name 'resolved' not defined
                         │   /api/system/*      │
                         │                      │
                         │   nginx proxy via    │
                         │   99.79.41.246       │
                         └──────────────────────┘
```

```
                         ┌────────────────────────────┐
                         │   3.96.151.28 (GPU box)    │
                         │   timesfm-api :8080        │ ← back online today
                         │                            │
                         │   cls_traj_14f_iso (5m)    │
                         │   15m_head_v1 (15m, but    │
                         │     dispatch broken — same │
                         │     value as 5m output)    │
                         │                            │
                         │   NOT consumed by engine — │
                         │   engine TIMESFM_URL       │
                         │   points at .v2 box still  │
                         └────────────────────────────┘
```

## What's currently being persisted

`strategy_decisions` table — confirmed populated:
- All 7 v15m strategies generating ~25k decisions/day (BTC)
- v7_15m_sniper_eth/sol/xrp generating ~8k decisions/day each
- Last 14d: 1.32M total 15m decisions across all strategies/assets

`market_data` table — confirmed:
- 1,302 / 1,303 BTC 15m windows resolved over last 14d (99.92%)
- ETH/SOL/XRP also at 99.9% resolution rate

`signal_evaluations` — STALE for 15m:
- BTC 5m: 400k+ rows last Apr 28
- BTC 15m: 1,514 rows, last Apr 24 22:27
- ETH/SOL/XRP: zero rows ever

The stale 15m `signal_evaluations` is consistent with `V2_SCORING_LOOPS_ENABLED=false` on both serving boxes. Predictions are computed on-demand via /v4/snapshot but not persisted to that table. **Live 15m signals exist but aren't persisted with their evaluation context** — strategy_decisions captures the outputs only.

## Trades (production)

Last 3,500 trades in DB (Apr 24-29):
- All BTC 5m
- 5 strategies: v9_lgb_only (1771), v10_lgb_only (1050), v9_ensemble (518), v8_champion_lgb_only (126), v8_champion (35)
- Aggregate WR ~75% (per `/api/trades/stats`: 1815W / 588L / 75.5%)
- Total PnL +$23.6k (cumulative all-time)

**Zero 15m trades. Zero non-BTC trades.**

## Pending audit tasks affecting 15m

Already open (from earlier diagnostic):

| # | P | Type | Title |
|---|---|---|---|
| #266 | 3 | infrastructure | TimesFM 15m missing ETH/SOL/XRP forecast models |
| #267 | 3 | engine-bug | data_surface.py /v4/snapshot cache is BTC-only |
| #250 | 7 | model_eval | Retrain path1 classifier - focal loss + label smoothing |
| #305 | 6 | ml_box | Expose VPIN scalar in /v4/snapshot.timescales.{5m,15m}.sub_signals |
| #329 | 5 | hub_bug | /api/v58/strategy-decisions returns name 'resolved' is not defined |

**New audit tasks this analysis recommends opening:**
- Restart policy + per-timescale classifier dispatch bug on GPU box
- Promote `cls_traj_14f_iso` to live primary
- Add `v15m_down_basic` strategy
- Build `v15m_consensus` meta-strategy
- Train v12-XL equivalent for 15m
- Hour-of-day filter applied to all 15m strategies
