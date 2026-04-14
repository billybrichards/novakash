# Engine Handover — 2026-04-14

## Current State

- Engine is currently in `PAPER` mode on Montreal.
- TimesFM service is back online and healthy.
- Strategy registry execution wiring bug has been fixed in code and pushed.
- Conservative `trade_advised` gating has been restored to the strategy YAMLs.
- Live trading is **not ready** to be re-enabled yet.

## What Happened

### 1. TimesFM instability

- TimesFM host: `3.98.114.0`
- Instance ID: `i-05dbd2ca41f8a75ec`
- Symptom: `/health` and `/v4/snapshot` timed out from both local and Montreal.
- AWS status at failure time: instance initially showed `impaired` and later became SSH-unreachable while EC2 health still showed `ok`.
- EC2 console output showed an OOM kill of uvicorn.

Relevant recovery:

```bash
aws ec2 stop-instances --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-stopped --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 start-instances --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-running --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-status-ok --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
```

Post-recovery verification:

- `docker compose ps` on `/home/ubuntu/timesfm-service`
- `curl http://localhost:8080/health`
- `curl http://localhost:8080/v4/health`
- memory observed healthy around `1.8 GiB / 7.6 GiB`

### 2. Live duplicate / oversize trade incident

The real issues were:

- `ExecuteTradeUseCase` was wired before `_window_state_repo` existed.
- This caused `execute_trade.dedup_check_error` and `execute_trade.mark_traded_error`.
- Duplicate orders were placed in the same window because dedup was effectively broken.
- Stake sizing used a stale bankroll path and a hardcoded `$50` cap in `ExecuteTradeUseCase`.
- That made the config look safe while the actual execution path was not.

Confirmed duplicate live window examples from Montreal:

- `btc-updown-5m-1776129300` / `v4_down_only` -> 5 orders, total stake `$186.00`
- `btc-updown-5m-1776129000` / `v4_down_only` -> 2 orders, total stake `$68.34`

### 3. Real live outcome

Confirmed resolved live trade:

- strategy: `v4_down_only`
- trade id: `4421`
- order id: `0xa493ae12aac099ac856960f3b35b21af034d3d3bfb6f564607f81ed61c76dc27`
- created: `2026-04-14 00:58:05+00`
- outcome: `LOSS`
- pnl: `-$2.32`

Current wallet check from Montreal via live Polymarket client:

- wallet balance: about `$29.62 USDC`

## Fixes Already Applied In Code

### Pushed commits

- `ff07708` — defer `ExecuteTradeUseCase` wiring until after DB/repo init
- `b5aee5b` — restore `trade_advised` gate on `v4_down_only`
- `b2aa97e` — enable all 5 strategies to `LIVE` with `trade_advised` in repo YAMLs

### Additional local changes ready to commit

1. `engine/config/runtime_config.py`

- map `absolute_max_bet` -> `max_position_usd`
- this makes the DB config's true hard cap flow into runtime

2. `engine/use_cases/execute_trade.py`

- remove stale hardcoded `$50` ceiling logic
- use `runtime.max_position_usd` as the real execution hard cap
- use `runtime.max_drawdown_kill` instead of the old baked-in constant

3. `engine/adapters/persistence/pg_trade_repo.py`

- stop misclassifying paper trades as live in the `trades` table
- derive `mode` and `is_live` from `execution_mode`

## Paper Overnight Findings

Paper mode did place trades overnight, but the persistence/reconciliation data is not yet trustworthy.

Observed using `execution_mode='paper'`:

- `v4_down_only`: 15 paper trades
- `v4_fusion`: 22 paper trades
- resolved wins: 0
- resolved losses: 0
- realized paper pnl: `$0.00`

Meaning:

- paper execution happened
- but paper resolution did not close out cleanly
- the overnight paper run cannot yet be used as a trustworthy win/loss proof

### Reconstructed Overnight Outcome Estimate

Using Montreal DB state, I matched paper trades against stored
`window_snapshots.actual_direction` by `window_ts` to estimate what the
overnight paper trades would have done if paper reconciliation had worked.

Estimated result over the last 8 hours:

- `v4_down_only`
  - trades: `13`
  - would win: `8`
  - would lose: `5`
  - implied PnL: `+$48.60`
- `v4_fusion`
  - trades: `25`
  - would win: `18`
  - would lose: `6`
  - unresolved: `1`
  - implied PnL: `+$121.80`

Interpretation:

- overnight paper signal quality looked good overall
- the main remaining problem is systems reliability, not obvious signal failure
- because the paper resolver is still broken, these should be treated as a
  reconstructed estimate, not a finalized audited paper PnL report

Useful matched examples:

- `v4_down_only`
  - `btc-updown-5m-1776152100` -> `DOWN` -> would win
  - `btc-updown-5m-1776150000` -> `DOWN` -> would win
  - `btc-updown-5m-1776144300` -> `DOWN` -> would win
- `v4_fusion`
  - many `NO/DOWN` windows would have won
  - a smaller set of `UP` outcome windows would have lost

## Montreal Ops Details

### Montreal engine box

- host: `15.223.247.178`
- instance id: `i-0785ed930423ae9fd`
- os user: `novakash`

### TimesFM box

- host: `3.98.114.0`
- instance id: `i-05dbd2ca41f8a75ec`
- os user: `ubuntu`

### Useful checks

Engine status:

```bash
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1
ssh -i /tmp/key novakash@15.223.247.178
```

TimesFM status:

```bash
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-05dbd2ca41f8a75ec \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/key.pub \
  --availability-zone ca-central-1d \
  --region ca-central-1
ssh -i /tmp/key ubuntu@3.98.114.0
cd /home/ubuntu/timesfm-service && docker compose ps
curl http://localhost:8080/health
curl http://localhost:8080/v4/health
```

## Session 2 Fixes (2026-04-14 Morning)

All fixes deployed to Montreal. Engine is on commit `67e1d00`.

### Bankroll / risk sizing fixed

- `runtime_config.py`: defaults changed — `STARTING_BANKROLL 500→29`, `max_position_usd 500→5`, `min_bet_usd 2→1`
- `execute_trade.py`: `MIN_BET_USD` constant `2→1`, now uses `min(constant, runtime.min_bet_usd)` so it's overridable
- `execute_trade.py`: hard cap now uses `runtime.max_position_usd` (was hardcoded `$50`)
- CI `.env` template: `STARTING_BANKROLL=30`, `PAPER_MODE=true`, `BET_FRACTION=0.07`, `MIN_BET_USD=1.0`, `MAX_POSITION_USD=5.0`
- GitHub Actions Variables set: `STARTING_BANKROLL=30`, `BET_FRACTION=0.07`, `MAX_POSITION_USD=5.0`
- With `$30` wallet at `7%` Kelly: `$30 × 0.07 = $2.10` → bumped to `5 shares × $0.48 = $2.40` actual. Hard cap `$5`.

### is_live=True bug found and fixed (root cause)

Wrong file was patched previously. Real write path:
```
ExecuteTradeUseCase → DBTradeRecorder → OrderManager._persist_trade → DBClient.write_trade()
```
`PgTradeRepository.record_trade` is only used by the Hub-side CLOB reconciler.

In `db_client.write_trade()` lines 272-273, `is_live` was derived from order ID prefix:
```python
# WRONG (old)
"live" if order.order_id.startswith("0x") else "paper",
not order.order_id.startswith("5min-") and not order.order_id.startswith("manual-paper"),
```
Registry paper trades get UUID order IDs (not `5min-` prefix) → `is_live=True` always.

Fix: derive from `execution_mode` in metadata (same as `pg_trade_repo.py`):
```python
# CORRECT (new)
"paper" if execution_mode == "paper" else "live",
execution_mode != "paper",
```

### Paper reconciliation fixed

`reconcile_positions.py` used stdlib `logging` with `extra={}` kwargs. Structlog
renderer swallows those fields, so all error detail was invisible in logs.
Switched to `structlog.get_logger()` — error detail now visible.

### DB paper config (Railway)

DB config `Paper Config v7.1` (id=26) overrides env vars on every sync:
- `bet_fraction`: `0.07 → 0.1` (DB has 0.1 — acceptable, 10% Kelly = `$3/trade`)
- `max_position_usd`: final value `5.0` ✅
- `daily_loss_limit_usd`: `50 → 80` (acceptable)

**Still needs manual update** on Railway to align `starting_bankroll=30`.
Use Hub API: `PUT /api/config {"starting_bankroll": 30}` or Hub UI Config page.
Hub credentials: user=`billy` / `HUB_ADMIN_PASSWORD` secret.

### CI path filter fixed

Removed dangerous exclusions (`engine/adapters/**`, `engine/use_cases/**`,
`engine/domain/**`, `engine/tests/**`) from deploy trigger paths. Previously a
fix in any of those directories would silently skip the deploy.

### 5-share minimum

Both `fok_ladder.py` and `polymarket_client.py` bump to `5 shares` (never reject).
At balanced `$0.48` DOWN market: `5 × $0.48 = $2.40` actual cost.

### Reconcile UC

`ENGINE_USE_RECONCILE_UC=true` in CI `.env` — wired and running.
`reconcile_uc.complete` firing every 2 min. `paper_resolved` will be non-zero
once first paper trade window closes and `window_snapshots.actual_direction` populated.

### Reconstructed overnight signal quality

From Montreal DB, matching paper trades against `window_snapshots.actual_direction`:
- `v4_down_only`: 8/13 = 61.5% WR (breakeven ~56%)
- `v4_fusion`: 18/25 = 72% WR

Both clear breakeven with margin. Reconstructed estimate, not audited fills.

## Go-Live Checklist

- [x] Dedup fixed (ExecuteTradeUseCase wired after DB init)
- [x] Hard cap `$5/trade` in code and CI env
- [x] `is_live` correctly derived from `execution_mode`
- [x] Paper trades mode/is_live columns correct
- [x] Paper reconciliation wired (ReconcilePositionsUseCase)
- [x] Paper resolve error detail visible in logs (structlog)
- [x] CI path filter no longer silently skips adapter/use_case fixes
- [x] GitHub Actions Variables: STARTING_BANKROLL=30, BET_FRACTION=0.07, MAX_POSITION_USD=5.0
- [ ] DB paper config `starting_bankroll` updated to `30` on Railway
- [ ] Confirm `reconcile_uc.complete paper_resolved > 0` after a full window
- [ ] Flip `PAPER_MODE=false` to go live (Hub UI system page or CI env)

## Bottom Line

Engine is in `PAPER` mode on Montreal with correct risk sizing for `$30` wallet.
Safe to observe paper trades resolve. Once `paper_resolved > 0` confirmed in logs,
flip to live via Hub system page. Kill switch fires at 45% drawdown (~`$13.50` loss).
